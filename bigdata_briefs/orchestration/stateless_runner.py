"""Database-less, single-entity run path.

Mirrors the core of ``run_entity_incremental`` but performs **no** database I/O:
no orchestration row, no run log, no lease/overlap checks, no post-run flushes, and
no embedding-novelty history. The caller supplies an explicit window; novelty is
search-only. The final report is returned directly from the graph's in-memory state.

This module deliberately imports nothing from the SQLite-backed orchestration models
or storages, so importing it never opens a database.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Semaphore
from typing import Any

import httpx

from bigdata_briefs.api.schemas import (
    StatelessBullet,
    StatelessCitation,
    StatelessEntityReport,
)
from bigdata_briefs.graph.dependencies import RuntimeDependencies
from bigdata_briefs.graph.graph import compile_brief_graph
from bigdata_briefs.graph.state import make_empty_state_defaults
from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.models import Entity
from bigdata_briefs.orchestration.kg_entities import (
    entity_from_kg_record,
    fetch_kg_entities_by_ids,
)
from bigdata_briefs.query_service.api import APIQueryService
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.service import BriefPipelineService
from bigdata_briefs.settings import UNSET, settings

# Coarse phase label for each graph node, surfaced as live progress during a run.
# Unknown nodes fall back to their raw node name.
_PHASE_BY_NODE: dict[str, str] = {
    "exploratory_search": "search",
    "quarter_info": "search",
    "concept_extraction": "search",
    "concept_search": "search",
    "concept_search_postprocessing": "search",
    "bullets_generation_and_scoring": "bullet_generation",
    "entity_grounding_check": "grounding",
    "novelty_search_parse_and_plan": "novelty",
    "novelty_search_fetch": "novelty",
    "novelty_search_judgment": "novelty",
    "novelty_search_rewrite": "novelty",
    "relevance_score_search": "novelty",
    "save_novel_bullets": "finalizing",
    "redundancy_check": "finalizing",
    "thematic_consolidation": "finalizing",
    "standalone_validation": "finalizing",
    "build_report": "finalizing",
}


def phase_for_node(node_name: str) -> str:
    """Map a graph node name to its coarse pipeline phase."""
    return _PHASE_BY_NODE.get(node_name, node_name)


def build_stateless_dependencies(
    *,
    rate_limiter: RequestsPerMinuteController | None = None,
    connection_sem: Semaphore | None = None,
    http_client: httpx.Client | None = None,
) -> RuntimeDependencies:
    """RuntimeDependencies with no engine and no persistent storage.

    ``chunk_filter_service=None`` disables cross-run chunk dedup (APIQueryService
    guards on it). ``brief_service`` is still built because Phase-1
    ``extract_concepts`` uses it; its embedding storage is ``None`` and never
    queried (the embedding-novelty nodes are absent from the stateless graph).
    """
    query_service = APIQueryService(
        chunk_filter_service=None,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )
    brief_service = BriefPipelineService.factory(embedding_storage=None)
    return RuntimeDependencies(
        engine=None,
        query_service=query_service,
        llm_client=brief_service.llm_client,
        brief_service=brief_service,
        novelty_service=brief_service.novelty_filter_service,
        embedding_client=None,
        embedding_storage=None,
        generated_bullet_storage=None,
        # Forward so search-novelty HTTP calls share the one process-global budget.
        bigdata_rate_limiter=query_service.rate_limit_controller,
    )


def resolve_entity_stateless(
    *,
    entity_id: str,
    rate_limiter: RequestsPerMinuteController | None = None,
    entity_metadata: dict[str, Any] | None = None,
) -> Entity:
    """Resolve the ``Entity`` with no DB cache.

    Uses caller-supplied ``entity_metadata`` (``{name, category, ticker, ...}``) when
    present to skip the network round-trip, else fetches live from the Knowledge Graph.
    """
    if entity_metadata:
        return entity_from_kg_record({"id": entity_id, **entity_metadata})
    api_key = (
        str(settings.BIGDATA_API_KEY) if settings.BIGDATA_API_KEY != UNSET else None
    )
    if not api_key:
        raise ValueError(
            "BIGDATA_API_KEY is not set; cannot resolve entity from Knowledge Graph"
        )
    results = fetch_kg_entities_by_ids(
        [entity_id],
        api_key=api_key,
        base_url=settings.API_BASE_URL,
        timeout_seconds=settings.API_TIMEOUT_SECONDS,
        rate_limiter=rate_limiter,
    )
    return entity_from_kg_record(results[entity_id])


# Discard-stage mapping mirrors orchestration.entity_runner._get_discard_stage and
# api.routes.reports._stage_to_category, kept here so the stateless path resolves the
# same stages without importing those DB-coupled modules.
_STAGE_CATEGORY: dict[str, str] = {
    "relevance_score": "relevance",
    "grounding": "grounding",
    "novelty_embedding": "novelty",
    "novelty_embedding_relevance": "novelty",
    "novelty_search": "novelty",
    "novelty_search_relevance": "novelty",
}


def _discard_category(bp: dict) -> str | None:
    """Map a discarded bullet record to relevance | grounding | novelty (or None)."""
    if bp.get("is_active", True):
        return None
    rs = bp.get("relevance_scoring") or {}
    if rs and not rs.get("passed", True):
        return _STAGE_CATEGORY["relevance_score"]
    eg = (bp.get("entity_grounding") or {}).get("check") or {}
    if eg.get("decision") == "invalid":
        return _STAGE_CATEGORY["grounding"]
    ns = bp.get("novelty_search") or {}
    s = ns.get("search") or {}
    if s.get("verdict") == "discard":
        return _STAGE_CATEGORY["novelty_search"]
    rc = ns.get("relevance_check") or {}
    if rc and not rc.get("passed", True):
        return _STAGE_CATEGORY["novelty_search_relevance"]
    return "novelty"  # discarded somewhere in the novelty phase; best-effort bucket


def _citation_lookup(final_state: dict) -> dict[str, dict]:
    """Index source_references by the "CQS:{document_id}-{chunk_id}" citation key."""
    lookup: dict[str, dict] = {}
    for src in (final_state.get("source_references") or {}).values():
        if isinstance(src, dict):
            doc_id = src.get("document_id")
            chunk_id = src.get("chunk_id")
            if doc_id is not None and chunk_id is not None:
                lookup[f"CQS:{doc_id}-{chunk_id}"] = src
    return lookup


def _build_entity_report(entity: Entity, final_state: dict) -> dict:
    """Build the per-entity report from the final graph state.

    Mirrors the /reports/bullets conventions: each published bullet is an object
    with its text + resolved citations + novelty decision attached; discarded
    bullets are grouped by stage. Citations expose only source_name / headline /
    url (no internal CQS id).
    """
    lookup = _citation_lookup(final_state)
    bullets: list[StatelessBullet] = []
    discarded: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}

    for bp in final_state.get("bullet_points") or []:
        if not bp.get("is_active", True):
            category = _discard_category(bp)
            text = bp.get("text") or ""
            if category and text:
                discarded[category].append(text)
            continue

        citations = [
            StatelessCitation(
                source_name=(lookup.get(cid) or {}).get("source_name") or "",
                headline=(lookup.get(cid) or {}).get("headline") or "",
                url=(lookup.get(cid) or {}).get("url"),
            )
            for cid in (bp.get("citations") or [])
        ]
        search = (bp.get("novelty_search") or {}).get("search") or {}
        verdict = search.get("verdict")
        bullets.append(
            StatelessBullet(
                text=bp.get("text") or "",
                citations=citations,
                search_action=verdict,
                # Fully novel unless kept with a mixed claim-level verdict
                # (at least one claim already known in the evidence).
                is_novel=not (
                    verdict == "keep"
                    and search.get("overall_verdict") == "novel_with_context"
                ),
            )
        )

    discarded_total = sum(
        1 for bp in (final_state.get("bullet_points") or []) if not bp.get("is_active", True)
    )

    return StatelessEntityReport(
        entity_id=entity.id,
        entity_name=entity.name,
        bullets_saved=len(bullets),
        bullets_discarded=discarded_total,
        bullets=bullets,
        discarded_by_relevance=discarded["relevance"],
        discarded_by_grounding=discarded["grounding"],
        discarded_by_novelty=discarded["novelty"],
    ).model_dump()


def run_entity_stateless(
    *,
    entity_id: str,
    window_start: datetime,
    window_end: datetime,
    pipeline_config: dict[str, Any],
    rate_limiter: RequestsPerMinuteController | None = None,
    connection_sem: Semaphore | None = None,
    http_client: httpx.Client | None = None,
    entity_metadata: dict[str, Any] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> dict:
    """Run the database-less pipeline for one entity and return its report dict.

    The returned value is a ``StatelessEntityReport`` dict: entity_name + per-bullet
    objects (text + resolved citations + novelty decision) + discarded-by-stage groups,
    mirroring the /reports/bullets conventions.

    When ``progress_cb`` is provided it is called with each coarse phase label
    ("search", "bullet_generation", "grounding", "novelty", "finalizing") as the
    graph advances, enabling live progress reporting for a fan-out job.
    """
    rs = window_start if window_start.tzinfo else window_start.replace(tzinfo=timezone.utc)
    re_ = window_end if window_end.tzinfo else window_end.replace(tzinfo=timezone.utc)
    if re_ <= rs:
        raise ValueError("window end must be after start")

    entity = resolve_entity_stateless(
        entity_id=entity_id,
        rate_limiter=rate_limiter,
        entity_metadata=entity_metadata,
    )
    deps = build_stateless_dependencies(
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )
    req = uuid.uuid4()
    deps.entity_metrics = EntityStepMetrics(entity.name)
    # deps.debug_logger stays None: no .brief_pipeline_state writes.

    initial_state = {
        **make_empty_state_defaults(),
        "entity_id": entity.id,
        "entity_name": entity.name,
        "entity_type": entity.entity_type,
        "entity_ticker": entity.ticker or "",
        "report_start_date": rs.isoformat(),
        "report_end_date": re_.isoformat(),
        "request_id": str(req),
        "config": pipeline_config,
    }
    graph = compile_brief_graph(stateless=True)
    config = {"configurable": {"deps": deps}}

    # Drive node-by-node so we can report progress ("updates"), and keep the full
    # state snapshot ("values") so we can read bullet_points + source_references for
    # the discarded count and resolved citations. The last "values" is the final state.
    final_state: dict = {}
    for mode, chunk in graph.stream(
        initial_state, config, stream_mode=["updates", "values"]
    ):
        if mode == "updates":
            if progress_cb is not None:
                for node_name in chunk:
                    progress_cb(phase_for_node(node_name))
        else:  # "values": full state snapshot; keep the latest
            final_state = chunk

    return _build_entity_report(entity, final_state)

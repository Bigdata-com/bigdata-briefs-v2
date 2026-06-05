"""
Node: save_novel_bullets

Final persistence node of the pipeline. Writes every bullet that arrives here
with `is_active=True` (i.e. survived all novelty + relevance checks) to the
`generated_bullet_points` table and sets `pipeline_status`.

This table is the authoritative record of bullets included in the report for
each run. No embeddings are stored here — those live in `sqlbulletpointembedding`
and can be linked via `trace_id`.

Saved per bullet:
  run_id, entity_id, entity_name, report_window_start/end, created_at,
  trace_id, text, citations, embedding_decision (KEEP|REWRITE),
  search_action (keep|rewrite|None).

Service type: none (DB write, no LLM or search API).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_SAVE_NOVEL_BULLETS,
    PIPELINE_STATUS_NO_DATA,
    PIPELINE_STATUS_RUNNING,
    SERVICE_TYPE_NONE,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.novelty.models import CitationDetail, GeneratedBulletPoint


def save_novel_bullet_points(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — save_novel_bullets.

    Persists the final bullet points (all active bullets at this stage) to the
    `generated_bullet_points` table and determines the pipeline status.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    run_id: str = state["request_id"]
    entity_id: str = state["entity_id"]
    entity_name: str = state["entity_name"]

    start_date = datetime.fromisoformat(state["report_start_date"])
    end_date = datetime.fromisoformat(state["report_end_date"])
    created_at = datetime.now(timezone.utc)

    bullet_points: list[dict] = state.get("bullet_points") or []

    # source_references keys are "CQS:REF{n}" (counter-based, from deduplicate_and_filter),
    # but bullet citations use "CQS:{document_id}-{chunk_id}" (from attribution/sources.py).
    # Build a reverse lookup indexed by the citation-format key so lookups work.
    raw_refs: dict = state.get("source_references") or {}
    citation_lookup: dict[str, dict] = {}
    for src in raw_refs.values():
        if isinstance(src, dict):
            doc_id = src.get("document_id")
            chunk_id = src.get("chunk_id")
            if doc_id is not None and chunk_id is not None:
                citation_lookup[f"CQS:{doc_id}-{chunk_id}"] = src

    to_store: list[GeneratedBulletPoint] = []
    for bp in bullet_points:
        record = bullet_to_record(bp)
        if not record.is_active:
            continue

        embedding_decision = None
        if record.novelty_embedding and record.novelty_embedding.judgment:
            embedding_decision = record.novelty_embedding.judgment.decision

        search_action = None
        not_fully_novel = False
        if record.novelty_search and record.novelty_search.search:
            search_action = record.novelty_search.search.verdict
            # Flag bullets that passed (keep) but have mixed claim novelty:
            # at least one claim was already known in the evidence.
            not_fully_novel = (
                search_action == "keep"
                and record.novelty_search.search.overall_verdict == "novel_with_context"
            )

        # Resolve citation IDs → {id, headline, text} using source_references.
        # Every ID should be resolvable (grounding already removed invalid refs),
        # but if one is missing we still persist it with empty fields so the data
        # is not silently lost.
        citation_details: list[CitationDetail] | None = None
        if record.citations:
            citation_details = [
                CitationDetail(
                    id=cit_id,
                    headline=(citation_lookup.get(cit_id) or {}).get("headline", ""),
                    text=(citation_lookup.get(cit_id) or {}).get("text", ""),
                    source_name=(citation_lookup.get(cit_id) or {}).get("source_name", ""),
                    url=(citation_lookup.get(cit_id) or {}).get("url"),
                )
                for cit_id in record.citations
            ]

        to_store.append(
            GeneratedBulletPoint(
                run_id=run_id,
                entity_id=entity_id,
                entity_name=entity_name,
                report_window_start=start_date,
                report_window_end=end_date,
                created_at=created_at,
                trace_id=record.trace_id,
                text=record.text,
                citations=citation_details,
                embedding_decision=embedding_decision,
                search_action=search_action,
                not_fully_novel=not_fully_novel,
            )
        )

    deps.generated_bullet_storage.store(to_store)

    active_count = len(to_store)
    pipeline_status = PIPELINE_STATUS_RUNNING if active_count > 0 else PIPELINE_STATUS_NO_DATA

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_SAVE_NOVEL_BULLETS,
        service_type=SERVICE_TYPE_NONE,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "saved_bullets": active_count,
            "pipeline_status": pipeline_status,
        },
    )

    return {
        "pipeline_status": pipeline_status,
        "node_metrics": [metrics.model_dump()],
    }

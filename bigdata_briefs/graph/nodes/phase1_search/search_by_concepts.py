"""
Node: concept_search

Runs raw parallel API queries — one per concept across all themes.
No deduplication, hash filtering, or reranking at this stage; those are
handled by the next node (concept_search_postprocessing).

Service type: search (parallel POST /v1/search per concept)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import NODE_CONCEPT_SEARCH, SERVICE_TYPE_SEARCH
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.models import ConceptExtraction, Entity, ReportDates
from bigdata_briefs.settings import settings


def execute_parallel_concept_queries(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — concept_search.

    For each concept in the extracted themes, fires a separate search query
    and collects the raw results grouped by theme and concept.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity = Entity(
        id=state["entity_id"],
        name=state["entity_name"],
        entity_type=state["entity_type"],
        ticker=state.get("entity_ticker") or None,
    )
    report_dates = ReportDates(
        start=state["report_start_date"],
        end=state["report_end_date"],
    )
    cfg = state.get("config") or {}

    concepts = ConceptExtraction.model_validate(state["extracted_concepts"])

    source_filter = cfg.get("source_filter")
    categories = cfg.get("categories")
    source_rank_boost = cfg.get("source_rank_boost")
    freshness_boost = cfg.get("freshness_boost")
    rerank_concept_sources = cfg.get("rerank_concept_sources", False)
    headline_search = cfg.get("headline_search", False)

    total_concepts = sum(len(cat.concepts) for cat in concepts.categories)

    # Size the pool to the shared connection semaphore. The 450 QPM limit is
    # enforced independently by RequestsPerMinuteController; oversubscribing
    # threads beyond the connection pool just wastes thread slots.
    with ThreadPoolExecutor(max_workers=settings.API_SIMULTANEOUS_REQUESTS) as executor:
        all_results, results_per_concept, results_by_theme = (
            deps.query_service.run_concept_queries_raw(
                entity=entity,
                concepts=concepts,
                report_dates=report_dates,
                source_filter=source_filter,
                categories=categories,
                executor=executor,
                source_rank_boost=source_rank_boost,
                freshness_boost=freshness_boost,
                rerank_concept_sources=rerank_concept_sources,
                debug_logger=deps.debug_logger,
                headline_search=headline_search,
            )
        )

    total_chunks = sum(len(r.chunks) for r in all_results) if all_results else 0

    if deps.entity_metrics:
        if total_chunks:
            deps.entity_metrics.track_chunks(total_chunks, attributee_step="concept_search")
        deps.entity_metrics.track_api_call(total_concepts, attributee_step="concept_search")

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_CONCEPT_SEARCH,
        service_type=SERVICE_TYPE_SEARCH,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        search_calls=total_concepts,
        extra={
            "result_count": len(all_results),
            "total_chunks": total_chunks,
            "theme_count": len(results_by_theme),
        },
    )

    raw_concept_results = {
        "all_results": [r.model_dump() for r in all_results],
        "results_per_concept": {
            concept: {
                "theme": data["theme"],
                "results": [r.model_dump() for r in data["results"]],
            }
            for concept, data in results_per_concept.items()
        },
        "results_by_theme": {
            theme: [r.model_dump() for r in theme_results]
            for theme, theme_results in results_by_theme.items()
        },
        # forward concepts so postprocessing can use them for reranking
        "concepts": concepts.model_dump(),
    }

    return {
        "raw_concept_results": raw_concept_results,
        "node_metrics": [metrics.model_dump()],
    }

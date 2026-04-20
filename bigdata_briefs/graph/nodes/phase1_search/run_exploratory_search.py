"""
Node: exploratory_search

Runs a broad multi-topic search to gather content chunks about the entity.
The retrieved chunks feed directly into concept extraction.

Note: quarter_info is intentionally split into its own node (fetch_quarter_info).
This node does NOT call the events-calendar API.

Service type: search (parallel POST /v1/search per topic)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_EXPLORATORY_SEARCH,
    PIPELINE_STATUS_NO_DATA,
    SERVICE_TYPE_SEARCH,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.models import Entity, ReportDates
from bigdata_briefs.settings import settings


def execute_broad_topic_search(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — exploratory_search.

    Executes parallel search queries for all configured topics. The retrieved
    ``Result`` objects are serialized and stored in ``exploratory_chunks``.

    Sets ``pipeline_status`` to ``"no_data"`` when nothing is found.
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

    topics = cfg.get("topics", [entity.name])
    source_filter = cfg.get("source_filter")
    categories = cfg.get("categories")
    source_rank_boost = cfg.get("source_rank_boost")
    freshness_boost = cfg.get("freshness_boost")

    # Size the pool to the shared connection semaphore. The 450 QPM limit is
    # enforced independently by RequestsPerMinuteController; oversubscribing
    # threads beyond the connection pool just wastes thread slots.
    with ThreadPoolExecutor(max_workers=settings.API_SIMULTANEOUS_REQUESTS) as executor:
        results = deps.query_service.run_exploratory_search(
            entity=entity,
            topics=topics,
            report_dates=report_dates,
            executor=executor,
            source_filter=source_filter,
            categories=categories,
            source_rank_boost=source_rank_boost,
            freshness_boost=freshness_boost,
            debug_logger=deps.debug_logger,
        )

    # Keep only rank-1 and rank-2 sources
    results = [r for r in results if r.source_rank in (1, 2)] if results else []

    has_results = bool(results)
    total_chunks = sum(len(r.chunks) for r in results) if results else 0

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_EXPLORATORY_SEARCH,
        service_type=SERVICE_TYPE_SEARCH,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        search_calls=len(topics),
        extra={"result_count": len(results) if results else 0, "total_chunks": total_chunks},
    )

    if not has_results:
        return {
            "exploratory_chunks": [],
            "pipeline_status": PIPELINE_STATUS_NO_DATA,
            "node_metrics": [metrics.model_dump()],
        }

    return {
        "exploratory_chunks": [r.model_dump() for r in results],
        "pipeline_status": "running",
        "node_metrics": [metrics.model_dump()],
    }

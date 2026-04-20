"""
Node: initial_check

Verifies that the entity has at least one result in the report date window
before running any expensive downstream processing.

Service type: search (single POST /v1/search with chunk_limit=1)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_INITIAL_CHECK,
    PIPELINE_STATUS_NO_DATA,
    SERVICE_TYPE_SEARCH,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.models import Entity, ReportDates


def verify_entity_has_search_results(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — initial_check.

    Performs a minimal search query (chunk_limit=1) to confirm the entity has
    data in the requested date window. Sets ``pipeline_status`` to ``"no_data"``
    if not, which routes the graph to END via the conditional edge.

    Returns partial state update with ``initial_check_result``,
    ``pipeline_status``, and one ``NodeMetricsRecord``.
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
    source_filter = cfg.get("source_filter")
    categories = cfg.get("categories")

    results = deps.query_service.check_if_entity_has_results(
        entity_id=entity.id,
        report_dates=report_dates,
        source_filter=source_filter,
        categories=categories,
        debug_logger=deps.debug_logger,
    )

    has_results = bool(results)
    result_count = len(results) if results else 0

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_INITIAL_CHECK,
        service_type=SERVICE_TYPE_SEARCH,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        search_calls=1,
        extra={"has_results": has_results, "result_count": result_count},
    )

    initial_check_result = {
        "has_results": has_results,
        "result_count": result_count,
    }
    pipeline_status = "running" if has_results else PIPELINE_STATUS_NO_DATA

    return {
        "initial_check_result": initial_check_result,
        "pipeline_status": pipeline_status,
        "node_metrics": [metrics.model_dump()],
    }

"""
Node: quarter_info

Fetches the current fiscal quarter title for the entity by querying the
earnings-calendar API. The title (e.g. "Q1 2026") is injected into bullet
generation and novelty prompts for temporal context.

Split from exploratory_search so each node owns a single service call.

Service type: search (POST /v1/events-calendar/query)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import NODE_QUARTER_INFO, SERVICE_TYPE_SEARCH
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.orchestration.earnings_calendar_cache import upsert_entity_earnings_calendar
from bigdata_briefs.settings import UNSET, settings
from bigdata_briefs.temporal import fetch_earnings_calendar_window


def resolve_fiscal_quarter_from_calendar(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — quarter_info.

    Queries the earnings-calendar endpoint for the entity and resolves the
    current fiscal quarter title. Falls back to empty string when the API key
    is unset or the call fails (non-blocking — the pipeline continues either way).
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_id = state["entity_id"]
    report_start = state["report_start_date"]

    # Convert report_start_date (ISO string) to a date object
    reference_date = datetime.fromisoformat(report_start[:10]).date()

    api_key = (
        settings.BIGDATA_API_KEY
        if settings.BIGDATA_API_KEY != UNSET
        else None
    )

    titles, events_by_entity = fetch_earnings_calendar_window(
        reference_date=reference_date,
        rp_entity_id=entity_id,
        api_key=api_key,
        rate_limiter=deps.bigdata_rate_limiter,
    )
    quarter_title: str = titles.get(entity_id) or ""

    # The earnings-calendar cache is a cross-run convenience; it is skipped in the
    # stateless path (deps.engine is None). The quarter title above is still
    # resolved from the live API either way.
    if api_key and deps.engine is not None:
        try:
            upsert_entity_earnings_calendar(
                deps.engine,
                entity_id,
                current_quarter_title=quarter_title,
                earnings_events=events_by_entity.get(entity_id, []),
                reference_as_of=reference_date,
            )
        except Exception:
            pass

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_QUARTER_INFO,
        service_type=SERVICE_TYPE_SEARCH,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        search_calls=1 if api_key else 0,
        extra={"quarter_title": quarter_title},
    )

    return {
        "current_quarter_title": quarter_title,
        "node_metrics": [metrics.model_dump()],
    }

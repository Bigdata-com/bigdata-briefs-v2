"""
Node execution logger for the Brief 2.0 LangGraph pipeline.

``with_node_log(node_id, fn)`` wraps any LangGraph node function to emit
structured START / DONE log lines via the package-level structlog logger.

Output format (structlog key=value rendering):
    [concept_extraction] START  entity=Microsoft Corp.
    [concept_extraction] DONE   entity=Microsoft Corp.  wall_ms=2341  categories=3  concepts_total=8
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from bigdata_briefs import logger
from bigdata_briefs.graph.state import BriefGraphState

def _format_window(start: str, end: str) -> str:
    """Return a compact human-readable window string from two ISO datetime strings.

    Same-day intra-day:  "2026-04-15 11:16→11:19"
    Multi-day:           "2026-04-14 11:16→2026-04-15 08:26"
    Missing:             ""
    """
    if not start or not end:
        return ""
    # Truncate to minute precision: "YYYY-MM-DDTHH:MM"
    s = start[:16].replace("T", " ")  # "2026-04-15 11:16"
    e = end[:16].replace("T", " ")    # "2026-04-15 11:19"
    s_date, s_time = s[:10], s[11:]
    e_date, e_time = e[:10], e[11:]
    if s_date == e_date:
        return f"{s_date} {s_time}→{e_time}"
    return f"{s_date} {s_time}→{e_date} {e_time}"


# Extra keys that are too verbose or uninteresting for the one-line DONE summary.
_SKIP_EXTRA_KEYS = {
    "schema_ensured",
    "total_source_refs",
}


def _summarise_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Return a cleaned-up subset of a node's extra metrics dict."""
    out: dict[str, Any] = {}
    for k, v in extra.items():
        if k in _SKIP_EXTRA_KEYS:
            continue
        # Always include skipped/reason — they tell us why nothing happened.
        if k in ("skipped", "reason"):
            out[k] = v
            continue
        # Drop falsy values (0, False, None, empty containers) unless it's
        # something we explicitly want even at zero.
        if not v and v != 0:
            continue
        out[k] = v
    return out


def with_node_log(node_id: str, fn: Callable) -> Callable:
    """Return a wrapped node function that emits START / DONE log lines.

    Works for both regular graph nodes and subgraph nodes
    (same ``(state, config) -> dict`` signature).
    """

    def _wrapped(state: BriefGraphState, config: RunnableConfig) -> dict:
        entity = state.get("entity_name") or state.get("entity_id") or "?"

        # Format the report window as a compact string, e.g. "2026-04-15 11:16→11:19"
        # or "2026-04-15→2026-04-16" for multi-day windows.
        window = _format_window(
            state.get("report_start_date") or "",
            state.get("report_end_date") or "",
        )

        # Contextual hint for the inner loop nodes
        ctx: dict[str, Any] = {}
        themes: list[str] = state.get("themes") or []
        idx: int = state.get("active_theme_index", 0)
        if node_id in ("bullets_generation", "relevance_score") and themes:
            ctx["theme"] = themes[idx] if idx < len(themes) else "?"

        logger.info(
            f"[{node_id}] START",
            entity=entity,
            window=window,
            **ctx,
        )

        t0 = time.monotonic()
        result: dict = fn(state, config)
        wall_ms = round((time.monotonic() - t0) * 1000)

        # Pull metrics out of the returned node_metrics list (first entry).
        metrics_list: list[dict] = (result or {}).get("node_metrics") or []
        first_metric: dict = metrics_list[0] if metrics_list else {}
        extra = _summarise_extra(first_metric.get("extra") or {})

        # Surface pipeline_status change if this node set it.
        if "pipeline_status" in (result or {}):
            extra["status"] = result["pipeline_status"]

        logger.info(
            f"[{node_id}] DONE",
            entity=entity,
            wall_ms=wall_ms,
            **{**ctx, **extra},
        )

        return result

    _wrapped.__name__ = fn.__name__
    _wrapped.__qualname__ = fn.__qualname__
    return _wrapped

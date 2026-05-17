"""Rank entities by day-over-day change in sentiment or media-attention z-score.

Uses sentiment_tool.py from the vendor/sentiment_tool directory (or the path
configured in SENTIMENT_TOOL_PATH). The tool is imported dynamically so updates
to the repo are picked up without changes to the briefs codebase.

To update: pull the latest version into vendor/sentiment_tool, or point
SENTIMENT_TOOL_PATH at your local clone.

Ranking metric options:
  "media_attention" (default) — |Δ chunks_zscore_mo|
  "sentiment"                 — |Δ sent_zscore_mo|
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

_METRIC_FIELD = {
    "media_attention": "chunks_zscore_mo",
    "sentiment":       "sent_zscore_mo",
}


def _load_sentiment_tool():
    """Import _fetch_volume and _compute_signals from sentiment_tool.py."""
    from bigdata_briefs.settings import settings

    tool_path = settings.SENTIMENT_TOOL_PATH
    if not os.path.isdir(tool_path):
        raise RuntimeError(
            f"sentiment_tool not found at {tool_path!r}. "
            "Clone the repo there or set SENTIMENT_TOOL_PATH."
        )

    if tool_path not in sys.path:
        sys.path.insert(0, tool_path)

    # Set env vars expected by sentiment_tool before import
    os.environ.setdefault("BIGDATA_API_KEY", settings.BIGDATA_API_KEY or "")
    os.environ.setdefault("OPENAI_API_KEY",  settings.OPENAI_API_KEY  or "")

    import importlib
    st = importlib.import_module("sentiment_tool")
    return st._fetch_volume, st._compute_signals


def _compute_delta(entity_id: str, metric_field: str, fetch_volume, compute_signals) -> tuple[str, float]:
    """Fetch volume, compute z-scores, return |Δ today - yesterday|."""
    try:
        df = compute_signals(fetch_volume(entity_id))
        if len(df) < 2:
            return entity_id, 0.0

        col = metric_field
        if col not in df.columns:
            return entity_id, 0.0

        today     = df[col].iloc[-1]
        yesterday = df[col].iloc[-2]

        import math
        if math.isnan(today) or math.isnan(yesterday):
            return entity_id, 0.0

        return entity_id, abs(float(today) - float(yesterday))

    except Exception as exc:
        logger.warning("sentiment_ranking: failed for %s: %s", entity_id, exc)
        return entity_id, 0.0


def rank_entities_by_signal(
    entity_ids: list[str],
    metric: str = "media_attention",
    workers: int = 10,
) -> list[str]:
    """Return entity_ids sorted descending by |Δ metric| (today vs yesterday).

    Args:
        entity_ids: list of entity IDs to rank
        metric:     "media_attention" (default) or "sentiment"
        workers:    parallel HTTP workers
    """
    field = _METRIC_FIELD.get(metric, "chunks_zscore_mo")

    fetch_volume, compute_signals = _load_sentiment_tool()

    scores: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_compute_delta, eid, field, fetch_volume, compute_signals): eid
            for eid in entity_ids
        }
        for fut in as_completed(futures):
            eid, delta = fut.result()
            scores[eid] = delta

    return sorted(entity_ids, key=lambda e: scores.get(e, 0.0), reverse=True)

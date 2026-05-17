"""Rank entities by day-over-day change in sentiment or media-attention z-score.

Uses the same methodology as sentiment_tool.py (Bigdata.com /v1/search/volume
+ EWM signals). Adapted for parallel execution inside the briefs pipeline.

Ranking metric options:
  "media_attention" (default) — |Δ chunks_zscore_mo|
  "sentiment"                 — |Δ sent_zscore_mo|

Returns a list of entity_ids sorted descending by the chosen metric's
day-over-day absolute change.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np

from bigdata_briefs.settings import settings

logger = logging.getLogger(__name__)

# ── Signal constants (mirror sentiment_tool.py) ───────────────────────────────
_LOOKBACK    = 90
_HL_SHORT    = 5
_HL_LONG     = 21
_WIN_MONTHLY = 30

_METRIC_FIELD = {
    "media_attention": "chunks_zscore_mo",
    "sentiment":       "sent_zscore_mo",
}


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _fetch_volume_raw(entity_id: str) -> list[dict]:
    """Call /v1/search/volume and return raw daily entries."""
    import httpx

    end_d   = date.today()
    start_d = end_d - timedelta(days=_LOOKBACK)
    body = {
        "query": {
            "auto_enrich_filters": False,
            "filters": {
                "timestamp": {
                    "start": f"{start_d.isoformat()}T00:00:00Z",
                    "end":   f"{end_d.isoformat()}T23:59:59Z",
                },
                "entity": {"any_of": [entity_id], "all_of": [], "none_of": []},
                "category": {"mode": "EXCLUDE", "values": ["my_files"]},
            },
        }
    }
    resp = httpx.post(
        f"{settings.API_BASE_URL}/v1/search/volume",
        json=body,
        headers={"X-API-KEY": settings.BIGDATA_API_KEY, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", {}).get("volume", [])


# ── Signal computation (numpy-only, no pandas) ────────────────────────────────

def _ewm(values: np.ndarray, halflife: float) -> np.ndarray:
    alpha = 1 - np.exp(-np.log(2) / halflife)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _rolling_zscore(arr: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(len(arr)):
        start = max(0, i - window + 1)
        chunk = arr[start : i + 1]
        if len(chunk) < min_periods:
            continue
        std = chunk.std()
        if std == 0 or np.isnan(std):
            continue
        out[i] = (arr[i] - chunk.mean()) / std
    return out


def _compute_delta(entity_id: str, metric_field: str) -> tuple[str, float]:
    """Fetch volume data, compute z-score for the chosen metric, return |Δ today-yesterday|."""
    try:
        raw = _fetch_volume_raw(entity_id)
        if not raw:
            return entity_id, 0.0

        # Build a date-indexed aligned series (90 days)
        end_d   = date.today()
        start_d = end_d - timedelta(days=_LOOKBACK)
        days = [start_d + timedelta(days=i) for i in range(_LOOKBACK + 1)]
        day_map: dict[date, dict] = {}
        for e in raw:
            d_str = e.get("date") or e.get("day")
            if d_str:
                d = date.fromisoformat(str(d_str)[:10])
                day_map[d] = e

        chunks    = np.array([day_map.get(d, {}).get("chunks", 0)    for d in days], dtype=float)
        sentiment = np.array([day_map.get(d, {}).get("sentiment", 0) for d in days], dtype=float)

        if metric_field == "chunks_zscore_mo":
            # Day-of-week normalisation
            dow = np.array([d.weekday() for d in days])
            dow_avg = np.array([
                chunks[dow == w].mean() if (dow == w).any() else 1.0
                for w in dow
            ])
            dow_avg = np.where(dow_avg == 0, 1.0, dow_avg)
            chunks_norm = chunks / dow_avg
            ewm_short   = _ewm(chunks_norm, _HL_SHORT)
            zscore      = _rolling_zscore(ewm_short, _WIN_MONTHLY, min_periods=7)
        else:  # sent_zscore_mo
            ewm_short = _ewm(sentiment, _HL_SHORT)
            zscore    = _rolling_zscore(ewm_short, _WIN_MONTHLY, min_periods=7)

        # Need at least 2 valid values at the end
        if len(zscore) < 2:
            return entity_id, 0.0
        today_val     = zscore[-1]
        yesterday_val = zscore[-2]
        if np.isnan(today_val) or np.isnan(yesterday_val):
            return entity_id, 0.0

        return entity_id, abs(float(today_val) - float(yesterday_val))

    except Exception as exc:
        logger.warning("sentiment_ranking: failed for %s: %s", entity_id, exc)
        return entity_id, 0.0


# ── Public API ────────────────────────────────────────────────────────────────

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
    scores: dict[str, float] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_compute_delta, eid, field): eid for eid in entity_ids}
        for fut in as_completed(futures):
            eid, delta = fut.result()
            scores[eid] = delta

    return sorted(entity_ids, key=lambda e: scores.get(e, 0.0), reverse=True)

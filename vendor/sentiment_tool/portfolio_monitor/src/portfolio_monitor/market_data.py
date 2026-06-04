"""Market data helpers — quote and price-change lookups via Bigdata.com."""

from __future__ import annotations

import time

from portfolio_monitor._compat import ensure_sentiment_tool_on_path

ensure_sentiment_tool_on_path()

from sentiment_tool import _bd_session, BASE_URL  # noqa: E402

_RETRY_STATUSES = {429, 503}
_MAX_RETRIES = 3


def _post_with_retry(url: str, payload: dict, timeout: int = 10) -> dict:
    """POST with exponential backoff on 429/503. Returns parsed JSON or {}."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _bd_session().post(url, json=payload, timeout=timeout)
            if resp.status_code in _RETRY_STATUSES:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == _MAX_RETRIES - 1:
                return {}
            time.sleep(2 ** attempt)
    return {}


def get_quote(entity_id: str) -> dict:
    """Fetch real-time quote for one entity.

    Returns a dict with keys: price, change_percentage, change, currency,
    market_cap, volume, open, previous_close, day_low, day_high, timestamp,
    exchange, name, target_identifier_id. Empty dict on failure.
    """
    data = _post_with_retry(
        f"{BASE_URL}/v1/quote/query",
        {"identifier": {"type": "rp_entity_id", "value": entity_id}},
    )
    results = data.get("results", [])
    return results[0] if results else {}


def get_price_changes(entity_id: str) -> dict:
    """Fetch historical price-change percentages for one entity.

    Returns a dict with keys: 1D, 5D, 1M, 3M, 6M, ytd, 1Y, 3Y, 5Y.
    Empty dict on failure.
    """
    data = _post_with_retry(
        f"{BASE_URL}/v1/price/changes/query",
        {"identifier": {"type": "rp_entity_id", "value": entity_id}},
    )
    results = data.get("results", [])
    return results[0] if results else {}

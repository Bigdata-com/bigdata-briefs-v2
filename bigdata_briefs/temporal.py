"""
Temporal / earnings-calendar helpers for contextual quarter information.

In the future this information could be obtained or stored more efficiently
(e.g. caching or pre-storing next quarters) to avoid an extra API call per run.
"""

from datetime import date, datetime
from typing import TYPE_CHECKING

import httpx

from bigdata_briefs.settings import UNSET, settings

if TYPE_CHECKING:
    from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController


def get_current_quarter_title(
    reference_date: date,
    rp_entity_id: str | list[str],
    *,
    api_key: str | None = None,
    start_date_offset_months: int = -6,
    end_date_offset_months: int = 4,
    api_url: str | None = None,
    rate_limiter: "RequestsPerMinuteController | None" = None,
    http_client: httpx.Client | None = None,
) -> dict[str, str | None]:
    """
    Fetch earnings-calendar events and infer the current quarter for each entity.

    "Current quarter" = the fiscal quarter of the next earnings call (e.g. if the
    next call is Q4 2025, we are in Q4 2025). When there is no future call in the
    window, uses the title of the most recent past call.

    Uses the workflow's BIGDATA_API_KEY when api_key is not provided.
    If the API key is unset, returns None for each entity.

    Parameters
    ----------
    reference_date : date
        As-of date (e.g. report date).
    rp_entity_id : str or list of str
        One or more company ids (e.g. "D8442A").
    api_key : str, optional
        X-API-KEY for the API. Defaults to settings.BIGDATA_API_KEY.
    start_date_offset_months : int
        Months to add to reference_date for start_date (default -6).
    end_date_offset_months : int
        Months to add to reference_date for end_date (default +4).
    api_url : str, optional
        Full events-calendar API URL. Defaults to {API_BASE_URL}/v1/events-calendar/query.

    Returns
    -------
    dict[str, str | None]
        Map rp_entity_id -> title of current quarter (e.g. "Q1 2026") or None
        if it cannot be determined or API key is unset.
    """
    try:
        from dateutil.relativedelta import relativedelta
    except ImportError:
        return {eid: None for eid in (rp_entity_id if isinstance(rp_entity_id, list) else [rp_entity_id])}

    if isinstance(rp_entity_id, str):
        rp_entity_id = [rp_entity_id]

    key = api_key if api_key is not None else (
        settings.BIGDATA_API_KEY if settings.BIGDATA_API_KEY != UNSET else None
    )
    if not key:
        return {eid: None for eid in rp_entity_id}

    base = (api_url or "").strip() or f"{settings.API_BASE_URL.rstrip('/')}/v1/events-calendar/query"
    start = reference_date + relativedelta(months=start_date_offset_months)
    end = reference_date + relativedelta(months=end_date_offset_months)

    payload = {
        "categories": ["earnings-call"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "limit": 100,
        "rp_entity_id": rp_entity_id,
    }
    headers = {
        "X-API-KEY": key,
        "Content-Type": "application/json",
    }

    try:
        # Every Bigdata HTTP call must pass through the shared limiter.
        if rate_limiter is not None:
            rate_limiter.acquire()
        if http_client is not None:
            # Shared FastAPI client: connection pool and headers already set.
            # Send an absolute URL since the shared client is bound to
            # API_BASE_URL already.
            response = http_client.post(base, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        else:
            with httpx.Client(timeout=settings.API_TIMEOUT_SECONDS) as client:
                response = client.post(base, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
    except Exception:
        return {eid: None for eid in rp_entity_id}

    results = data.get("results") or {}
    out: dict[str, str | None] = {}

    for eid in rp_entity_id:
        events = results.get(eid) or []
        if not events:
            out[eid] = None
            continue

        events = sorted(events, key=lambda e: e["event_datetime"])

        ref_ts = datetime.combine(reference_date, datetime.min.time())
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.replace(tzinfo=datetime.now().astimezone().tzinfo)

        next_call = None
        for ev in events:
            ev_dt = datetime.fromisoformat(ev["event_datetime"].replace("Z", "+00:00"))
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=ref_ts.tzinfo)
            if ev_dt > ref_ts:
                next_call = ev
                break

        if next_call is None:
            title = events[-1]["title"]
        else:
            # Current quarter = the quarter of the next call (e.g. next call Q4 2025 → we are in Q4 2025)
            fy = next_call["fiscal_year"]
            fp = next_call["fiscal_period"]
            title = next_call.get("title") or f"{fp} {fy}"

        out[eid] = title

    return out

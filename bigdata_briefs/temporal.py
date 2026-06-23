"""
Temporal / earnings-calendar helpers for contextual quarter information.

Per-entity snapshots are persisted to ``SQLEntityEarningsCalendar`` when the
pipeline runs ``quarter_info``; the Brief front page reads from DB instead of
re-querying the calendar for every date change.
"""

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from bigdata_briefs.settings import UNSET, settings

if TYPE_CHECKING:
    from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController


def _events_calendar_query(
    start_d: date,
    end_d: date,
    rp_entity_ids: list[str],
    *,
    api_key: str,
    api_url: str | None,
    rate_limiter: "RequestsPerMinuteController | None",
    http_client: httpx.Client | None,
) -> dict[str, Any]:
    """POST /v1/events-calendar/query and return parsed JSON (or {} on failure)."""
    base = (api_url or "").strip() or f"{settings.API_BASE_URL.rstrip('/')}/v1/events-calendar/query"
    payload = {
        "categories": ["earnings-call"],
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "limit": 100,
        "rp_entity_id": rp_entity_ids,
    }
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        if rate_limiter is not None:
            rate_limiter.acquire()
        if http_client is not None:
            response = http_client.post(base, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        with httpx.Client(timeout=settings.API_TIMEOUT_SECONDS) as client:
            response = client.post(base, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except Exception:
        return {}


def fetch_earnings_calendar_window(
    reference_date: date,
    rp_entity_id: str | list[str],
    *,
    api_key: str | None = None,
    start_date_offset_months: int = -6,
    end_date_offset_months: int = 4,
    api_url: str | None = None,
    rate_limiter: "RequestsPerMinuteController | None" = None,
    http_client: httpx.Client | None = None,
) -> tuple[dict[str, str | None], dict[str, list[dict[str, Any]]]]:
    """One calendar API call: quarter titles plus normalized events per entity (for DB cache)."""
    try:
        from dateutil.relativedelta import relativedelta
    except ImportError:
        ids = [rp_entity_id] if isinstance(rp_entity_id, str) else list(rp_entity_id)
        return {eid: None for eid in ids}, {eid: [] for eid in ids}

    if isinstance(rp_entity_id, str):
        rp_entity_id = [rp_entity_id]

    key = api_key if api_key is not None else (
        settings.BIGDATA_API_KEY if settings.BIGDATA_API_KEY != UNSET else None
    )
    if not key:
        return {eid: None for eid in rp_entity_id}, {eid: [] for eid in rp_entity_id}

    start = reference_date + relativedelta(months=start_date_offset_months)
    end = reference_date + relativedelta(months=end_date_offset_months)
    data = _events_calendar_query(
        start,
        end,
        list(rp_entity_id),
        api_key=key,
        api_url=api_url,
        rate_limiter=rate_limiter,
        http_client=http_client,
    )
    if not data:
        return {eid: None for eid in rp_entity_id}, {eid: [] for eid in rp_entity_id}

    results = data.get("results") or {}
    titles: dict[str, str | None] = {}
    events_by: dict[str, list[dict[str, Any]]] = {}

    for eid in rp_entity_id:
        events = results.get(eid) or []
        if not events:
            titles[eid] = None
            events_by[eid] = []
            continue

        events_sorted = sorted(events, key=lambda e: e.get("event_datetime") or "")

        ref_ts = datetime.combine(reference_date, datetime.min.time())
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.replace(tzinfo=datetime.now().astimezone().tzinfo)

        next_call = None
        for ev in events_sorted:
            try:
                ev_dt = datetime.fromisoformat(ev["event_datetime"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError):
                continue
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=ref_ts.tzinfo)
            if ev_dt > ref_ts:
                next_call = ev
                break

        if next_call is None:
            last = events_sorted[-1]
            title = last.get("title")
            if not title:
                fy, fp = last.get("fiscal_year"), last.get("fiscal_period")
                title = f"{fp} {fy}" if fp and fy else None
        else:
            fy = next_call.get("fiscal_year")
            fp = next_call.get("fiscal_period")
            title = next_call.get("title") or (f"{fp} {fy}" if fp and fy else None)

        titles[eid] = title
        events_by[eid] = [
            {
                "event_datetime": e.get("event_datetime"),
                "fiscal_year": e.get("fiscal_year"),
                "fiscal_period": e.get("fiscal_period"),
                "title": e.get("title"),
            }
            for e in events_sorted
            if isinstance(e, dict)
        ]

    return titles, events_by


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
    """
    titles, _ = fetch_earnings_calendar_window(
        reference_date,
        rp_entity_id,
        api_key=api_key,
        start_date_offset_months=start_date_offset_months,
        end_date_offset_months=end_date_offset_months,
        api_url=api_url,
        rate_limiter=rate_limiter,
        http_client=http_client,
    )
    return titles


def map_earnings_calls_on_calendar_day(
    calendar_day: date,
    rp_entity_ids: list[str],
    *,
    api_key: str | None = None,
    chunk_size: int = 80,
    api_url: str | None = None,
    rate_limiter: "RequestsPerMinuteController | None" = None,
    http_client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """For each entity id, detect an earnings-call event on ``calendar_day`` (UTC calendar date).

    Matches how the Brief date rail uses plain YYYY-MM-DD: event ``event_datetime`` is
    converted to UTC and compared to ``calendar_day``.

    Returns
    -------
    dict[str, dict]
        ``entity_id -> {"on_date": bool, "session_title": str | None}``
    """
    ids = list(dict.fromkeys(rp_entity_ids))
    out: dict[str, dict[str, Any]] = {
        eid: {"on_date": False, "session_title": None} for eid in ids
    }
    if not ids:
        return out

    key = api_key if api_key is not None else (
        settings.BIGDATA_API_KEY if settings.BIGDATA_API_KEY != UNSET else None
    )
    if not key:
        return out

    for i in range(0, len(ids), max(1, chunk_size)):
        chunk = ids[i : i + chunk_size]
        data = _events_calendar_query(
            calendar_day,
            calendar_day,
            chunk,
            api_key=key,
            api_url=api_url,
            rate_limiter=rate_limiter,
            http_client=http_client,
        )
        results = data.get("results") or {}
        for eid in chunk:
            events = results.get(eid) or []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                raw = ev.get("event_datetime")
                if not raw:
                    continue
                try:
                    ev_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                ev_day = ev_dt.astimezone(timezone.utc).date()
                if ev_day == calendar_day:
                    title = ev.get("title")
                    if not title:
                        fy = ev.get("fiscal_year")
                        fp = ev.get("fiscal_period")
                        title = f"{fp} {fy}" if fp and fy else None
                    out[eid] = {"on_date": True, "session_title": title}
                    break

    return out

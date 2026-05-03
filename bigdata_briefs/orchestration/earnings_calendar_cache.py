"""Persist and read per-entity earnings-calendar snapshots (see SQLEntityEarningsCalendar)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session

from bigdata_briefs.orchestration.models import SQLEntityEarningsCalendar


def _normalize_event(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_datetime": ev.get("event_datetime"),
        "fiscal_year": ev.get("fiscal_year"),
        "fiscal_period": ev.get("fiscal_period"),
        "title": ev.get("title"),
    }


def normalize_earnings_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_event(e) for e in events if isinstance(e, dict)]


def upsert_entity_earnings_calendar(
    engine: Engine,
    entity_id: str,
    *,
    current_quarter_title: str,
    earnings_events: list[dict[str, Any]],
    reference_as_of: date,
) -> None:
    """Replace the cached row for this entity (non-fatal errors swallowed by caller)."""
    now = datetime.now(timezone.utc)
    payload = json.dumps(normalize_earnings_events(earnings_events))
    title = (current_quarter_title or "").strip() or None
    with Session(engine) as session:
        row = session.get(SQLEntityEarningsCalendar, entity_id)
        if row is None:
            row = SQLEntityEarningsCalendar(
                entity_id=entity_id,
                current_quarter_title=title,
                earnings_events_json=payload,
                reference_as_of=reference_as_of.isoformat(),
                updated_at=now,
            )
            session.add(row)
        else:
            row.current_quarter_title = title
            row.earnings_events_json = payload
            row.reference_as_of = reference_as_of.isoformat()
            row.updated_at = now
        session.commit()


def earnings_flags_for_calendar_day(events_json: str, calendar_day: date) -> tuple[bool, str | None]:
    """True if any cached event falls on ``calendar_day`` (UTC date of event_datetime)."""
    try:
        events = json.loads(events_json or "[]")
    except Exception:
        return False, None
    if not isinstance(events, list):
        return False, None
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
        if ev_dt.astimezone(timezone.utc).date() != calendar_day:
            continue
        title = ev.get("title")
        if not title:
            fy = ev.get("fiscal_year")
            fp = ev.get("fiscal_period")
            title = f"{fp} {fy}" if fp and fy else None
        return True, title
    return False, None

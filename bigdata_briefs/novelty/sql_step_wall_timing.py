"""SQLite persistence for pipeline step wall timings."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel

if TYPE_CHECKING:
    from bigdata_briefs.metrics import EntityStepMetrics


class SQLPipelineStepWallTiming(SQLModel, table=True):
    """One wall-clock timing row: pipeline step (and optional novelty substep)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    request_id: str = Field(index=True)
    entity_id: str | None = Field(default=None, index=True)
    entity_name: str | None = None
    calendar_day: str | None = Field(default=None, index=True)
    pipeline_step: str = Field(index=True)
    substep: str | None = Field(default=None, index=True)
    started_at_utc: datetime
    ended_at_utc: datetime
    duration_seconds: float


def persist_step_wall_timings(engine: Engine, rows: list[SQLPipelineStepWallTiming]) -> None:
    """Insert timing rows; failures should be caught by the caller."""
    if not rows:
        return
    with Session(engine) as session:
        for row in rows:
            session.add(row)
        session.commit()


def sql_rows_from_entity_metrics(
    entity_metrics: "EntityStepMetrics",
    *,
    request_id: str,
    entity_id: str | None,
    entity_name: str | None,
    calendar_day: str | None,
) -> list[SQLPipelineStepWallTiming]:
    """Build ORM rows from :meth:`EntityStepMetrics.get_step_wall_timings_for_db`."""
    out: list[SQLPipelineStepWallTiming] = []
    for r in entity_metrics.get_step_wall_timings_for_db():
        started = r["started_at_utc"]
        ended = r["ended_at_utc"]
        if not isinstance(started, datetime) or not isinstance(ended, datetime):
            continue
        dur = r.get("duration_seconds")
        if not isinstance(dur, (int, float)):
            dur = (ended - started).total_seconds()
        out.append(
            SQLPipelineStepWallTiming(
                request_id=request_id,
                entity_id=entity_id,
                entity_name=entity_name,
                calendar_day=calendar_day,
                pipeline_step=str(r["pipeline_step"]),
                substep=(str(r["substep"]) if r.get("substep") is not None else None),
                started_at_utc=started,
                ended_at_utc=ended,
                duration_seconds=float(dur),
            )
        )
    return out


def calendar_day_iso_utc(report_end: datetime) -> str:
    """Calendar date in UTC from a report boundary datetime."""
    dt = report_end
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def flush_step_wall_timings_to_sqlite(
    engine: Engine,
    entity_metrics: "EntityStepMetrics",
    *,
    request_id: str,
    entity_id: str | None,
    entity_name: str | None,
    calendar_day: str | None,
) -> None:
    """Persist wall timings; raises on DB errors (caller may catch)."""
    rows = sql_rows_from_entity_metrics(
        entity_metrics,
        request_id=request_id,
        entity_id=entity_id,
        entity_name=entity_name,
        calendar_day=calendar_day,
    )
    persist_step_wall_timings(engine, rows)

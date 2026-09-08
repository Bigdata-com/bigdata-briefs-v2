"""Rolling retention prune for the live Briefs SQLite database.

Targets heavy history tables only. Does **not** touch portfolio briefs,
orchestration KG cache, earnings calendar, or user portfolio. Does **not**
delete ``/data/snapshot.db`` (ops one-shot outside this module).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, func, select, text
from sqlalchemy.engine import Engine
from sqlmodel import Session

from bigdata_briefs.novelty.sql_models import (
    SQLBulletPointEmbedding,
    SQLChunkTextHash,
    SQLGeneratedBulletPoint,
)
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint
from bigdata_briefs.novelty.sql_step_wall_timing import SQLPipelineStepWallTiming
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityPipelineRunLog,
    SQLEntitySignalHistory,
    SQLRunMetrics,
    SQLRunNarrative,
)
from bigdata_briefs.settings import settings


@dataclass
class RetentionPruneResult:
    """Counts of rows removed (or that would be removed in dry-run)."""

    cutoff_utc: datetime
    keep_days: int
    dry_run: bool
    vacuum: bool
    deleted: dict[str, int] = field(default_factory=dict)

    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def default_keep_days() -> int:
    """Prefer novelty lookback so prune does not undercut novelty history."""
    return max(30, int(settings.NOVELTY_LOOKBACK_DAYS))


def retention_cutoff(*, keep_days: int, now: datetime | None = None) -> datetime:
    """Return timezone-aware UTC cutoff: rows strictly older than this are pruned."""
    if keep_days < 1:
        raise ValueError(f"keep_days must be >= 1, got {keep_days}")
    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    day = (ref - timedelta(days=keep_days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    # SQLModel/SQLAlchemy may return a one-element Row for scalar selects.
    if hasattr(value, "__getitem__") and not isinstance(value, (str, bytes)):
        return uuid.UUID(str(value[0]))  # type: ignore[index]
    return uuid.UUID(str(value))


def _count_where(session: Session, model: type, *clauses: object) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in clauses:
        stmt = stmt.where(clause)  # type: ignore[arg-type]
    return int(session.scalar(stmt) or 0)


def prune_live_database(
    engine: Engine,
    *,
    keep_days: int = 30,
    dry_run: bool = True,
    vacuum: bool = False,
    now: datetime | None = None,
) -> RetentionPruneResult:
    """Prune history older than ``keep_days`` from the live DB bound to ``engine``.

    When ``dry_run`` is True, no rows are deleted and ``vacuum`` is ignored.
    """
    cutoff = retention_cutoff(keep_days=keep_days, now=now)
    cutoff_date = cutoff.date().isoformat()
    deleted: dict[str, int] = {}

    with Session(engine) as session:
        run_ids = [
            _as_uuid(r)
            for r in session.exec(
                select(SQLEntityPipelineRunLog.run_id).where(
                    SQLEntityPipelineRunLog.report_window_end < cutoff
                )
            ).all()
        ]
        str_run_ids = [str(r) for r in run_ids]

        planned: dict[str, int] = {
            "sqlbulletrunlog": (
                _count_where(session, SQLBulletRunLog, SQLBulletRunLog.run_id.in_(run_ids))
                if run_ids
                else 0
            ),
            "sqlrunmetrics": (
                _count_where(session, SQLRunMetrics, SQLRunMetrics.run_id.in_(run_ids))
                if run_ids
                else 0
            ),
            "sqlrunnarrative": (
                _count_where(session, SQLRunNarrative, SQLRunNarrative.run_id.in_(run_ids))
                if run_ids
                else 0
            ),
            "generated_bullet_points": (
                _count_where(
                    session,
                    SQLGeneratedBulletPoint,
                    SQLGeneratedBulletPoint.run_id.in_(str_run_ids),
                )
                if str_run_ids
                else 0
            ),
            "sqlentitypipelinerunlog": len(run_ids),
            "sqlbulletpointembedding": _count_where(
                session, SQLBulletPointEmbedding, SQLBulletPointEmbedding.date < cutoff
            ),
            "sqlchunktexthash": _count_where(
                session, SQLChunkTextHash, SQLChunkTextHash.date < cutoff
            ),
            "sqlbulletpipelinecheckpoint": _count_where(
                session,
                SQLBulletPipelineCheckpoint,
                SQLBulletPipelineCheckpoint.report_date < cutoff,
            ),
            "sqlentitysignalhistory": _count_where(
                session,
                SQLEntitySignalHistory,
                SQLEntitySignalHistory.date < cutoff_date,
            ),
            "sqlpipelinestepwalltiming": _count_where(
                session,
                SQLPipelineStepWallTiming,
                SQLPipelineStepWallTiming.calendar_day < cutoff_date,
            ),
        }

        if dry_run:
            return RetentionPruneResult(
                cutoff_utc=cutoff,
                keep_days=keep_days,
                dry_run=True,
                vacuum=False,
                deleted=planned,
            )

        if run_ids:
            for model, key in (
                (SQLBulletRunLog, "sqlbulletrunlog"),
                (SQLRunMetrics, "sqlrunmetrics"),
                (SQLRunNarrative, "sqlrunnarrative"),
            ):
                session.exec(sa_delete(model).where(model.run_id.in_(run_ids)))
                deleted[key] = planned[key]
            session.exec(
                sa_delete(SQLGeneratedBulletPoint).where(
                    SQLGeneratedBulletPoint.run_id.in_(str_run_ids)
                )
            )
            deleted["generated_bullet_points"] = planned["generated_bullet_points"]
            session.exec(
                sa_delete(SQLEntityPipelineRunLog).where(
                    SQLEntityPipelineRunLog.run_id.in_(run_ids)
                )
            )
            deleted["sqlentitypipelinerunlog"] = planned["sqlentitypipelinerunlog"]
        else:
            deleted["sqlbulletrunlog"] = 0
            deleted["sqlrunmetrics"] = 0
            deleted["sqlrunnarrative"] = 0
            deleted["generated_bullet_points"] = 0
            deleted["sqlentitypipelinerunlog"] = 0

        for model, key, clause in (
            (
                SQLBulletPointEmbedding,
                "sqlbulletpointembedding",
                SQLBulletPointEmbedding.date < cutoff,
            ),
            (SQLChunkTextHash, "sqlchunktexthash", SQLChunkTextHash.date < cutoff),
            (
                SQLBulletPipelineCheckpoint,
                "sqlbulletpipelinecheckpoint",
                SQLBulletPipelineCheckpoint.report_date < cutoff,
            ),
            (
                SQLEntitySignalHistory,
                "sqlentitysignalhistory",
                SQLEntitySignalHistory.date < cutoff_date,
            ),
            (
                SQLPipelineStepWallTiming,
                "sqlpipelinestepwalltiming",
                SQLPipelineStepWallTiming.calendar_day < cutoff_date,
            ),
        ):
            session.exec(sa_delete(model).where(clause))
            deleted[key] = planned[key]

        session.commit()

    if vacuum:
        # VACUUM cannot run inside a transaction on SQLite.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM"))

    return RetentionPruneResult(
        cutoff_utc=cutoff,
        keep_days=keep_days,
        dry_run=False,
        vacuum=vacuum,
        deleted=deleted,
    )

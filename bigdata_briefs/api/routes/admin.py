"""
Routes: utilities

    POST /api/v1/utilities/reset-db          → drop and recreate all tables (DESTRUCTIVE)
    POST /api/v1/utilities/clear-stale-runs  → reset stuck ``running`` rows to ``failed``
    POST /api/v1/utilities/delete-date       → delete all pipeline data for a calendar date
    POST /api/v1/utilities/prune-retention   → rolling-month prune of heavy history tables
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import SQLModel, Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import (
    ClearStaleRunsResponse,
    DeleteDateResponse,
    PruneRetentionResponse,
    ResetDatabaseResponse,
)
from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog
from bigdata_briefs.orchestration.retention import default_keep_days, prune_live_database

router = APIRouter(tags=["utilities"])


@router.post(
    "/utilities/reset-db",
    response_model=ResetDatabaseResponse,
    dependencies=[Depends(require_api_key)],
    summary="Reset the entire database",
    description=(
        "**DESTRUCTIVE — irreversible.** Drops every table managed by the pipeline "
        "(embeddings, generated bullets, run logs, orchestration state, checkpoints, "
        "chunk hashes, step timings) and recreates them empty.\n\n"
        "Pass `confirm=true` in the request body to execute; omitting it or passing "
        "`false` returns a 400 so accidental calls are rejected."
    ),
)
def reset_database(confirm: bool = False) -> ResetDatabaseResponse:
    """Drop all pipeline tables and recreate them from scratch."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Pass confirm=true to execute the reset. "
                "This operation is irreversible and will delete all data."
            ),
        )

    engine = get_engine()

    # Collect table names before dropping (metadata is already populated by imports
    # in ensure_orchestration_schema / db.py)
    table_names: list[str] = sorted(SQLModel.metadata.tables.keys())

    SQLModel.metadata.drop_all(engine)
    ensure_orchestration_schema(engine)

    recreated: list[str] = sorted(SQLModel.metadata.tables.keys())

    return ResetDatabaseResponse(
        tables_dropped=table_names,
        tables_recreated=recreated,
        total_tables=len(recreated),
    )


@router.post(
    "/utilities/clear-stale-runs",
    response_model=ClearStaleRunsResponse,
    dependencies=[Depends(require_api_key)],
    summary="Clear stuck 'running' run-log rows",
    description=(
        "Resets any ``SQLEntityPipelineRunLog`` row whose ``status`` is ``'running'`` "
        "and whose ``process_started_at_utc`` is older than ``stale_seconds`` seconds "
        "ago. Rows are transitioned to ``'failed'`` with ``error_summary='stale running "
        "lease cleared'``.\n\n"
        "Use this to unblock entities that were left in ``running`` state because the "
        "server was force-killed mid-run.\n\n"
        "**Default** (``stale_seconds=0``): clears **all** running rows immediately, "
        "regardless of age — useful after a restart."
    ),
)
def clear_stale_runs(
    stale_seconds: int = Query(
        default=0,
        ge=0,
        description=(
            "Age threshold in seconds. Running rows older than this are cleared. "
            "Pass 0 (default) to clear all running rows unconditionally."
        ),
    ),
) -> ClearStaleRunsResponse:
    """Reset stuck running rows to failed."""
    engine = get_engine()
    now = datetime.now(timezone.utc)
    cleared_ids: list[str] = []

    with Session(engine) as session:
        stmt = select(SQLEntityPipelineRunLog).where(
            SQLEntityPipelineRunLog.status == "running"
        )
        rows = session.exec(stmt).all()
        for row in rows:
            started = row.process_started_at_utc
            if started is None:
                age = float("inf")
            else:
                # Ensure timezone-aware comparison
                if started.tzinfo is None:
                    from datetime import timezone as _tz
                    started = started.replace(tzinfo=_tz.utc)
                age = (now - started).total_seconds()
            if age >= stale_seconds:
                row.status = "failed"
                row.process_completed_at_utc = now
                row.error_summary = "stale running lease cleared"
                session.add(row)
                cleared_ids.append(row.entity_id)
        session.commit()

    return ClearStaleRunsResponse(
        cleared=len(cleared_ids),
        entity_ids=cleared_ids,
        stale_seconds_threshold=stale_seconds,
    )


@router.post(
    "/utilities/delete-date",
    response_model=DeleteDateResponse,
    dependencies=[Depends(require_api_key)],
    summary="Delete all pipeline data for a calendar date",
    description=(
        "**DESTRUCTIVE — irreversible.** Removes every pipeline artifact generated on "
        "the given calendar date (matched via ``report_window_end``): run logs, bullet "
        "logs, embeddings, checkpoints, chunk hashes, step timings, signal history, "
        "portfolio briefs, and narratives.\n\n"
        "Date must be in ``YYYY-MM-DD`` format."
    ),
)
def delete_date(date: str = Query(..., description="Calendar date to delete (YYYY-MM-DD)")) -> DeleteDateResponse:
    from bigdata_briefs.api.routes.ui import _delete_date_data
    engine = get_engine()
    runs_deleted = _delete_date_data(engine, date)
    return DeleteDateResponse(date=date, runs_deleted=runs_deleted)


@router.post(
    "/utilities/prune-retention",
    response_model=PruneRetentionResponse,
    dependencies=[Depends(require_api_key)],
    summary="Prune pipeline history older than a rolling retention window",
    description=(
        "Removes heavy history older than ``keep_days`` (default aligned with "
        "``NOVELTY_LOOKBACK_DAYS``, minimum 30): embeddings, chunk hashes, bullet/"
        "pipeline run logs, generated bullets, metrics, narratives, checkpoints, "
        "signal history, and step wall timings.\n\n"
        "Does **not** delete portfolio briefs, orchestration KG cache, earnings "
        "calendar, or user portfolio.\n\n"
        "Defaults to ``dry_run=true`` (counts only). Pass ``dry_run=false`` to "
        "delete. Pass ``vacuum=true`` after a real prune to reclaim SQLite pages."
    ),
)
def prune_retention(
    keep_days: int | None = Query(
        default=None,
        ge=1,
        description="Retention window in days. Default: max(30, NOVELTY_LOOKBACK_DAYS).",
    ),
    dry_run: bool = Query(
        default=True,
        description="If true (default), report counts without deleting.",
    ),
    vacuum: bool = Query(
        default=False,
        description="Run SQLite VACUUM after prune (ignored when dry_run=true).",
    ),
) -> PruneRetentionResponse:
    days = keep_days if keep_days is not None else default_keep_days()
    engine = get_engine()
    result = prune_live_database(
        engine,
        keep_days=days,
        dry_run=dry_run,
        vacuum=vacuum and not dry_run,
    )
    return PruneRetentionResponse(
        cutoff_utc=result.cutoff_utc,
        keep_days=result.keep_days,
        dry_run=result.dry_run,
        vacuum=result.vacuum,
        deleted=result.deleted,
        total_deleted=result.total_deleted(),
    )

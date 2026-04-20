"""
Routes: admin operations

    POST /api/v1/admin/reset-db          → drop and recreate all tables (DESTRUCTIVE)
    POST /api/v1/admin/clear-stale-runs  → reset stuck ``running`` rows to ``failed``
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import SQLModel, Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import ClearStaleRunsResponse, ResetDatabaseResponse
from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog
from bigdata_briefs.settings import settings

router = APIRouter(tags=["admin"])


@router.post(
    "/admin/reset-db",
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
    "/admin/clear-stale-runs",
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

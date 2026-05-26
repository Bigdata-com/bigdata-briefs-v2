"""
Routes: run lifecycle

    GET  /api/v1/runs/{run_id}  → fetch run status / metadata
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import RunStatusResponse
from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog

router = APIRouter(tags=["runs"])


@router.get(
    "/runs/{run_id}",
    response_model=RunStatusResponse,
    dependencies=[Depends(require_api_key)],
    summary="Get run status",
)
def get_run_status(run_id: uuid.UUID) -> RunStatusResponse:
    """Return the status and window metadata for a specific pipeline run."""
    with Session(get_engine()) as session:
        row = session.get(SQLEntityPipelineRunLog, run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found.",
        )
    error_message: str | None = None
    error_traceback: str | None = None
    if row.error_summary:
        parts = row.error_summary.split("\n\n", 1)
        error_message = parts[0]
        error_traceback = parts[1] if len(parts) > 1 else None

    return RunStatusResponse(
        run_id=str(row.run_id),
        entity_id=row.entity_id,
        status=row.status,
        window_start=row.report_window_start,
        window_end=row.report_window_end,
        started_at=row.process_started_at_utc,
        completed_at=row.process_completed_at_utc,
        error_message=error_message,
        error_traceback=error_traceback,
        exit_code=row.exit_code,
    )



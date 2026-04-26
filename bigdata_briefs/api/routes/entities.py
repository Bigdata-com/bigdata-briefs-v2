"""
Routes: entity-scoped operations

    GET  /api/v1/entities/{entity_id}/runs       → paginated run history
    POST /api/v1/entities/{entity_id}/dry-run    → preview window + previous bullets
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func
from sqlmodel import Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.citation_mapping import stored_citation_dict_to_detail
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import (
    BulletPointItem,
    DeleteEntityResponse,
    DryRunRequest,
    DryRunResponse,
    EntityRunsResponse,
    LatestBulletsResponse,
    RunSummary,
)
from bigdata_briefs.novelty.sql_models import (
    SQLBulletPointEmbedding,
    SQLChunkTextHash,
    SQLGeneratedBulletPoint,
)
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint
from bigdata_briefs.novelty.sql_step_wall_timing import SQLPipelineStepWallTiming
from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)
from bigdata_briefs.settings import settings

router = APIRouter(tags=["entities"])


def _default_state_dir() -> Path:
    env = settings.BRIEF_PIPELINE_STATE_DIR.strip()
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".brief_pipeline_state"


@router.get(
    "/entities/{entity_id}/runs",
    response_model=EntityRunsResponse,
    dependencies=[Depends(require_api_key)],
    summary="List past runs for an entity",
)
def list_entity_runs(
    entity_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EntityRunsResponse:
    """Return paginated run history for the entity, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        total: int = session.exec(
            select(func.count(SQLEntityPipelineRunLog.run_id)).where(
                SQLEntityPipelineRunLog.entity_id == entity_id
            )
        ).one()
        rows = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
            .offset(offset)
            .limit(limit)
        ).all()

    return EntityRunsResponse(
        entity_id=entity_id,
        total=total,
        runs=[
            RunSummary(
                run_id=str(r.run_id),
                status=r.status,
                window_start=r.report_window_start,
                window_end=r.report_window_end,
                started_at=r.process_started_at_utc,
                completed_at=r.process_completed_at_utc,
                error_message=r.error_summary.split("\n\n", 1)[0] if r.error_summary else None,
                exit_code=r.exit_code,
            )
            for r in rows
        ],
    )


@router.get(
    "/entities/{entity_id}/bullets",
    response_model=LatestBulletsResponse,
    dependencies=[Depends(require_api_key)],
    summary="Get bullet points from the latest run for an entity",
    description=(
        "Returns all bullet points saved during the most recent successful run "
        "for the entity, along with run-level temporal details and per-bullet citations."
    ),
)
def get_latest_bullets(entity_id: str) -> LatestBulletsResponse:
    """Return bullets from the latest completed run for the given entity."""
    storage = SQLiteGeneratedBulletPointStorage(get_engine())
    rows = storage.get_latest_run_bullets(entity_id)

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No bullets found for entity '{entity_id}'.",
        )

    first = rows[0]
    return LatestBulletsResponse(
        entity_id=first.entity_id,
        entity_name=first.entity_name,
        run_id=first.run_id,
        report_window_start=first.report_window_start,
        report_window_end=first.report_window_end,
        run_created_at=first.created_at,
        bullet_count=len(rows),
        bullets=[
            BulletPointItem(
                trace_id=row.trace_id,
                text=row.text,
                citations=[
                    stored_citation_dict_to_detail(c)
                    for c in (row.citations or [])
                    if isinstance(c, dict)
                ],
                embedding_decision=row.embedding_decision,
                search_action=row.search_action,
                not_fully_novel=row.not_fully_novel or False,
            )
            for row in rows
        ],
    )


@router.delete(
    "/entities/{entity_id}",
    response_model=DeleteEntityResponse,
    dependencies=[Depends(require_api_key)],
    summary="Delete all data for an entity",
    description=(
        "Permanently removes every row associated with the given entity across "
        "all tables: embeddings, generated bullets, chunk hashes, pipeline "
        "checkpoints, step timings, orchestration state, and run logs."
    ),
)
def delete_entity(entity_id: str) -> DeleteEntityResponse:
    """Purge all persisted data for an entity from every table."""
    from sqlalchemy import delete as sa_delete

    engine = get_engine()
    tables: list[tuple[str, type]] = [
        ("sqlbulletpointembedding", SQLBulletPointEmbedding),
        ("generated_bullet_points", SQLGeneratedBulletPoint),
        ("sqlchunktexthash", SQLChunkTextHash),
        ("sqlbulletpipelinecheckpoint", SQLBulletPipelineCheckpoint),
        ("sqlpipelinestepwalltiming", SQLPipelineStepWallTiming),
        ("sqlentityorchestrationstate", SQLEntityOrchestrationState),
        ("sqlentitypipelinerunlog", SQLEntityPipelineRunLog),
    ]

    deleted: dict[str, int] = {}
    with Session(engine) as session:
        for table_name, model in tables:
            result = session.exec(
                sa_delete(model).where(model.entity_id == entity_id)
            )
            deleted[table_name] = result.rowcount
        session.commit()

    return DeleteEntityResponse(
        entity_id=entity_id,
        deleted=deleted,
        total_deleted=sum(deleted.values()),
    )


@router.post(
    "/entities/{entity_id}/dry-run",
    response_model=DryRunResponse,
    dependencies=[Depends(require_api_key)],
    summary="Preview report window and previous bullets",
    description=(
        "Computes the report window for the entity without executing the pipeline. "
        "Returns the window dates and any bullets produced in previous runs that fall "
        "within the same window."
    ),
)
def dry_run(
    entity_id: str,
    body: DryRunRequest,
) -> DryRunResponse:
    result = run_entity_incremental(
        entity_id=entity_id,
        pipeline_config={},
        state_dir=_default_state_dir(),
        dry_run=True,
        force_window_start=body.force_window_start,
        force_window_end=body.force_window_end,
        window_mode=body.window_mode,
        engine=get_engine(),
    )
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.error or "Dry run failed.",
        )
    return DryRunResponse(
        entity_id=entity_id,
        window_start=result.report_dates.start,
        window_end=result.report_dates.end,
        previous_bullets=result.previous_bullets,
    )

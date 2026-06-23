"""
Routes: entity-scoped operations

    GET    /api/v1/entities/{entity_id}/runs     → paginated run history
    GET    /api/v1/entities/{entity_id}/bullets  → latest run bullets
    DELETE /api/v1/entities/{entity_id}          → purge all entity data
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func
from sqlmodel import Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import (
    DeleteEntityResponse,
    EntityRunsResponse,
    RunSummary,
)
from bigdata_briefs.novelty.sql_models import (
    SQLBulletPointEmbedding,
    SQLChunkTextHash,
    SQLGeneratedBulletPoint,
)
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint
from bigdata_briefs.novelty.sql_step_wall_timing import SQLPipelineStepWallTiming
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)

router = APIRouter(tags=["entities"])


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



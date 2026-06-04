"""
Routes: batch operations

    POST /api/v1/batch/run-parallel          → run pipeline in parallel for a list of entities
    GET  /api/v1/batch/parallel/{id}/status  → status of a parallel batch run
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock, Semaphore

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs import logger
from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_engine,
    get_entity_executor,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.api.schemas import (
    BatchParallelRunResponse,
    BatchParallelRunStatusItem,
    BatchParallelRunStatusResponse,
    BatchRunRequest,
)
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import SQLBatchParallelRun, SQLEntityPipelineRunLog
from bigdata_briefs.api.routes.universes import _UNIVERSES
from bigdata_briefs.settings import settings


def _all_entity_ids(engine) -> list[str]:
    """Return all distinct entity_ids that have at least one run log row."""
    with Session(engine) as session:
        rows = session.exec(
            select(SQLEntityPipelineRunLog.entity_id).distinct()
        ).all()
    return list(rows)


router = APIRouter(tags=["batch"])


def _resolve_state_dir(state_dir: str | None) -> Path:
    if state_dir:
        return Path(state_dir).expanduser()
    env = settings.BRIEF_PIPELINE_STATE_DIR.strip()
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".brief_pipeline_state"


def _assert_no_running_entities(entity_ids: list[str]) -> None:
    """Raise HTTP 409 if any entity in the list has an active (status=running) run.

    This is a pre-flight guard checked before queuing any background work.
    It prevents accidentally launching a second batch while a previous one is
    still in progress for the same entities.
    """
    with Session(get_engine()) as session:
        busy: list[str] = []
        for entity_id in entity_ids:
            row = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.status == "running")
                .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
            ).first()
            if row is not None:
                busy.append(entity_id)

    if busy:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "entities_busy",
                "message": (
                    f"{len(busy)} entity/entities already have an active run. "
                    "Wait for them to complete or check /api/v1/batch/status."
                ),
                "busy_entity_ids": busy,
            },
        )



def _run_one_entity_safely(
    *,
    run_id: uuid.UUID,
    entity_id: str,
    pipeline_config: dict,
    state_dir: Path,
    force_run: bool,
    force_window_start,
    force_window_end,
    window_mode,
    force_overlap: bool,
    generate_narrative: bool,
    engine: Engine,
    rate_limiter: RequestsPerMinuteController,
    connection_sem: Semaphore,
    http_client: httpx.Client,
    startup_delay_seconds: float = 0.0,
) -> None:
    """Executor-safe wrapper.

    Any exception raised by ``run_entity_incremental`` must be caught here or
    it poisons the ``Future`` result and — more importantly — can silently kill
    the worker without leaving a useful trace. We log with full stack and swallow,
    because one bad entity run must not take the remaining entities down.
    The stale ``running`` row (if any) will be reaped by the existing finalizer
    in ``entity_runner`` on the next run.

    ``startup_delay_seconds`` is only applied to the first MAX_CONCURRENT_ENTITIES
    workers to spread the initial burst. Entities queued behind those already wait
    naturally for a free slot, so no extra delay is needed for them.
    """
    if startup_delay_seconds > 0:
        import time as _time
        _time.sleep(startup_delay_seconds)
    try:
        run_entity_incremental(
            run_id=run_id,
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            force_run=force_run,
            force_window_start=force_window_start,
            force_window_end=force_window_end,
            window_mode=window_mode,
            force_overlap=force_overlap,
            generate_narrative=generate_narrative,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
        )
    except Exception:
        logger.exception(
            "Parallel entity run failed",
            entity_id=entity_id,
            run_id=str(run_id),
        )


@router.post(
    "/batch/run-parallel",
    response_model=BatchParallelRunResponse,
    dependencies=[Depends(require_api_key)],
    summary="Run pipeline in parallel for multiple entities",
    description=(
        "Submits entity IDs (or a named universe) to the pipeline. All entities run "
        "concurrently up to `MAX_CONCURRENT_ENTITIES`. Returns a single **batch_id** to monitor "
        "progress via **GET /api/v1/batch/parallel/{batch_id}/status**.\n\n"
        "**Entity resolution** — provide `entity_ids`, a `universe`, or neither. "
        "When neither is provided the pipeline runs on all entities tracked in the database.\n\n"
        "**Date window** — omit `force_window_start` / `force_window_end` to use the automatic "
        "incremental window controlled by `window_mode`. Pass explicit ISO 8601 dates to target a "
        "specific period. One day at a time is recommended: a single-day window produces sharper "
        "bullets and more reliable novelty comparisons. Wider windows can generate briefs with "
        "ambiguous temporal references for high-volume entities.\n\n"
        "**Window modes** (when no forced dates are provided):\n"
        "- `continuous` *(default)* — covers `[end of last run → now]`, picking up exactly where "
        "the previous run stopped. Falls back to `[UTC midnight of today → now]` if no previous "
        "run exists. Guarantees no gaps across consecutive runs.\n"
        "- `update` — covers at most the 24 hours preceding now (72h on Mondays to bridge the "
        "weekend gap). Starts from the last run's end if it falls within that window.\n\n"
        "**Overlap protection** — if the requested window overlaps an already-completed run for "
        "the same entity, that entity is rejected immediately with an error and marked as `failed` "
        "in the batch status. No API or LLM calls are made for that entity."
    ),
)
def batch_run_parallel(
    body: BatchRunRequest,
    executor: ThreadPoolExecutor = Depends(get_entity_executor),
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
) -> BatchParallelRunResponse:
    # Resolve entity_ids: explicit list, named universe, or all DB entities
    if body.universe:
        if body.entity_ids:
            raise HTTPException(
                status_code=422,
                detail="Provide either 'entity_ids' or 'universe', not both.",
            )
        if body.universe == "my_portfolio":
            from bigdata_briefs.api.routes.universes import _get_my_portfolio_ids
            entity_ids = _get_my_portfolio_ids()
        else:
            entity_ids = _UNIVERSES.get(body.universe)
        if entity_ids is None:
            raise HTTPException(
                status_code=404,
                detail=f"Universe '{body.universe}' not found. Available: {list(_UNIVERSES) + ['my_portfolio']}",
            )
    elif body.entity_ids:
        entity_ids = body.entity_ids
    else:
        # No entity_ids and no universe — run all entities tracked in the database.
        # Typical use: window_mode=continuous with no explicit scope = full portfolio resume.
        entity_ids = _all_entity_ids(get_engine())

    if not entity_ids:
        raise HTTPException(status_code=422, detail="No entity_ids to run.")

    _assert_no_running_entities(entity_ids)

    cfg_path = resolve_config_path(None)
    pipeline_config = load_pipeline_config_dict(cfg_path)
    if body.categories:
        pipeline_config["categories"] = body.categories
    state_dir = _resolve_state_dir(None)
    run_ids = [uuid.uuid4() for _ in entity_ids]
    engine = get_engine()
    batch_id = uuid.uuid4()
    submitted_at = datetime.utcnow()
    total = len(entity_ids)

    # Persist batch record so the status endpoint can resolve run IDs later
    run_ids_map = {eid: str(rid) for eid, rid in zip(entity_ids, run_ids)}
    with Session(engine) as session:
        session.add(SQLBatchParallelRun(
            batch_id=batch_id,
            submitted_at=submitted_at,
            total=total,
            entity_ids_json=json.dumps(entity_ids),
            run_ids_json=json.dumps(run_ids_map),
        ))
        session.commit()

    # Stagger only the first MAX_CONCURRENT_ENTITIES workers — they start immediately
    # and would all hammer the API at once. Entities beyond that are queued in the
    # ThreadPoolExecutor and already wait naturally for a free slot.
    _ENTITY_STAGGER_SECONDS = 0.5
    _completed = [0]
    _lock = Lock()

    def _on_entity_done(_future):
        with _lock:
            _completed[0] += 1
            if _completed[0] == total:
                logger.info(
                    "Batch run-parallel complete",
                    batch_id=str(batch_id),
                    total=total,
                    entity_ids=entity_ids,
                )
                # After all entities finish: signals first, then portfolio brief from DB
                import threading as _threading
                _ranking_metric = body.ranking_metric
                _batch_run_ids  = list(run_ids)

                def _post_batch_pipeline():
                    try:
                        from sqlmodel import Session as _Session, select as _select
                        from sqlalchemy import desc as _desc
                        from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog as _RunLog
                        from bigdata_briefs.orchestration.sentiment_ranking import compute_and_store_signals
                        from bigdata_briefs.orchestration.portfolio_brief import generate_and_store_portfolio_brief

                        compute_and_store_signals(engine, entity_ids)
                        logger.info("Signal history stored", batch_id=str(batch_id))

                        with _Session(engine) as _s:
                            latest = _s.exec(
                                _select(_RunLog)
                                .where(_RunLog.run_id.in_(_batch_run_ids))
                                .where(_RunLog.status.in_(["succeeded", "no_data"]))
                                .order_by(_desc(_RunLog.report_window_end))
                            ).first()
                        if latest and latest.report_window_end and _ranking_metric is not None:
                            date_iso = latest.report_window_end.date().isoformat()
                            generate_and_store_portfolio_brief(
                                engine, date_iso, top_n=5,
                                ranking_metric=_ranking_metric,
                            )
                    except Exception:
                        logger.exception("Post-batch pipeline (signals + portfolio brief) failed")
                        return
                    try:
                        from bigdata_briefs.api.app import invalidate_desk_cache
                        invalidate_desk_cache()
                    except Exception:
                        logger.exception("Desk cache invalidation after batch failed")

                _threading.Thread(target=_post_batch_pipeline, daemon=True).start()

    for idx, (run_id, entity_id) in enumerate(zip(run_ids, entity_ids)):
        future = executor.submit(
            _run_one_entity_safely,
            run_id=run_id,
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            force_run=False,
            force_window_start=body.force_window_start,
            force_window_end=body.force_window_end,
            window_mode=body.window_mode,
            force_overlap=body.force_overlap,
            generate_narrative=body.generate_narrative,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
            startup_delay_seconds=idx * _ENTITY_STAGGER_SECONDS if idx < settings.MAX_CONCURRENT_ENTITIES else 0.0,
        )
        future.add_done_callback(_on_entity_done)

    logger.info(
        "Batch run-parallel submitted",
        batch_id=str(batch_id),
        total=total,
        entity_ids=entity_ids,
        universe=body.universe,
    )
    return BatchParallelRunResponse(
        batch_id=str(batch_id),
        total=total,
        submitted_at=submitted_at,
    )


@router.get(
    "/batch/parallel/{batch_id}/status",
    response_model=BatchParallelRunStatusResponse,
    dependencies=[Depends(require_api_key)],
    summary="Status of a parallel batch run",
    description=(
        "Returns the real-time status of a batch submitted via **POST /api/v1/batch/run-parallel**: "
        "how many entities have succeeded, failed, are still running, or have not started yet. "
        "Poll this endpoint until `running` reaches 0 to know when the batch is fully complete."
    ),
)
def batch_parallel_status(batch_id: uuid.UUID) -> BatchParallelRunStatusResponse:
    engine = get_engine()

    with Session(engine) as session:
        batch = session.get(SQLBatchParallelRun, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    entity_ids: list[str] = json.loads(batch.entity_ids_json)
    run_ids_map: dict[str, str] = json.loads(batch.run_ids_json)

    runs: list[BatchParallelRunStatusItem] = []
    succeeded = failed = running = not_started = 0

    with Session(engine) as session:
        for entity_id in entity_ids:
            run_id_str = run_ids_map.get(entity_id, "")
            try:
                run_uuid = uuid.UUID(run_id_str)
            except ValueError:
                runs.append(BatchParallelRunStatusItem(
                    entity_id=entity_id, run_id=run_id_str, status="not_started",
                ))
                not_started += 1
                continue

            row = session.get(SQLEntityPipelineRunLog, run_uuid)
            if row is None:
                runs.append(BatchParallelRunStatusItem(
                    entity_id=entity_id, run_id=run_id_str, status="not_started",
                ))
                not_started += 1
                continue

            error_msg: str | None = None
            if row.status == "failed" and row.error_summary:
                error_msg = row.error_summary.splitlines()[0]

            runs.append(BatchParallelRunStatusItem(
                entity_id=entity_id,
                run_id=run_id_str,
                status=row.status,
                started_at=row.process_started_at_utc,
                completed_at=row.process_completed_at_utc,
                error_message=error_msg,
            ))

            if row.status == "succeeded":
                succeeded += 1
            elif row.status == "failed":
                failed += 1
            elif row.status == "running":
                running += 1

    return BatchParallelRunStatusResponse(
        batch_id=str(batch_id),
        submitted_at=batch.submitted_at,
        total=batch.total,
        succeeded=succeeded,
        failed=failed,
        running=running,
        not_started=not_started,
        runs=runs,
    )


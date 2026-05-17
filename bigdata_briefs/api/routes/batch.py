"""
Routes: batch operations

    POST /api/v1/batch/run      → run pipeline sequentially for a list of entities
    POST /api/v1/batch/status   → health-check on a batch: how many done, errors, etc.
    POST /api/v1/batch/bullets  → return latest-run bullets for a list of entities
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock, Semaphore

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
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
    BatchBulletsDetailRequest,
    BatchBulletsDetailResponse,
    BatchBulletsRequest,
    BatchBulletsResponse,
    BatchParallelRunResponse,
    BatchParallelRunStatusItem,
    BatchParallelRunStatusResponse,
    BatchRunRequest,
    BatchRunResponse,
    BatchRunStatusItem,
    BatchStatusRequest,
    BatchStatusResponse,
    BulletDetailItem,
    BulletDiscardDetail,
    BulletPassedDetail,
    BulletPointItem,
    CitationDetail,
    ClaimVerdictDetail,
    EvidenceDetail,
    EntityBulletsResult,
    EntityDetailResult,
    RunBulletsResult,
    RunDetailResult,
    RunSubmittedResponse,
)
from bigdata_briefs.novelty.sql_models import SQLGeneratedBulletPoint
from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import SQLBatchParallelRun, SQLBulletRunLog, SQLEntityOrchestrationState, SQLEntityPipelineRunLog
from bigdata_briefs.api.routes.universes import _UNIVERSES
from bigdata_briefs.settings import settings


def _all_entity_ids(engine) -> list[str]:
    """Return all distinct entity_ids that have at least one run log row."""
    with Session(engine) as session:
        rows = session.exec(
            select(SQLEntityPipelineRunLog.entity_id).distinct()
        ).all()
    return list(rows)


def _stage_to_category(discard_stage: str | None) -> str | None:
    """Map SQLBulletRunLog.discard_stage to the three batch API categories."""
    if discard_stage == "relevance_score":
        return "relevance"
    if discard_stage == "grounding":
        return "grounding"
    if discard_stage in (
        "novelty_embedding", "novelty_embedding_relevance",
        "novelty_search", "novelty_search_relevance",
    ):
        return "novelty"
    return None


def _classify_discarded(bp: dict) -> str | None:
    """
    Return the discard category for an inactive bullet, or None if still active.

    Priority order mirrors the pipeline stages:
      1. relevance  — relevance_scoring.passed == False
      2. grounding  — entity_grounding.check.decision == "invalid"
      3. novelty    — embedding judgment/relevance or search verdict/relevance
    """
    if bp.get("is_active", True):
        return None

    # 1. Relevance gate
    rs = bp.get("relevance_scoring") or {}
    if rs and not rs.get("passed", True):
        return "relevance"

    # 2. Entity grounding
    eg_check = (bp.get("entity_grounding") or {}).get("check") or {}
    if eg_check.get("decision") == "invalid":
        return "grounding"

    # 3. Novelty — embedding path
    ne = bp.get("novelty_embedding") or {}
    judgment = ne.get("judgment") or {}
    if judgment.get("decision") == "discard":
        return "novelty"
    emb_rc = ne.get("relevance_check") or {}
    if emb_rc and not emb_rc.get("passed", True):
        return "novelty"

    # 3. Novelty — search path
    ns = bp.get("novelty_search") or {}
    search = ns.get("search") or {}
    if search.get("verdict") == "discard":
        return "novelty"
    search_rc = ns.get("relevance_check") or {}
    if search_rc and not search_rc.get("passed", True):
        return "novelty"

    # Catch-all: node failure or unknown — treat as novelty
    return "novelty"


def _load_discarded_for_runs(
    run_info: dict[str, tuple[str, datetime, datetime]],
) -> dict[str, dict[str, list[str]]]:
    """
    For each generated run_id, find the matching SQLEntityPipelineRunLog row by
    (entity_id, report_window_start, report_window_end), then read discarded
    bullets from SQLBulletRunLog instead of parsing output_json.

    NOTE: ``SQLGeneratedBulletPoint.run_id`` == ``state["request_id"]`` (internal UUID),
    which is *different* from ``SQLEntityPipelineRunLog.run_id`` (API-level UUID).
    We therefore match by entity + window, not by UUID.

    Args:
        run_info: { generated_run_id -> (entity_id, window_start, window_end) }

    Returns: { generated_run_id -> { "relevance": [...], "grounding": [...], "novelty": [...] } }
    """
    if not run_info:
        return {}

    result: dict[str, dict[str, list[str]]] = {}
    engine = get_engine()

    with Session(engine) as session:
        for run_id_str, (entity_id, window_start, window_end) in run_info.items():
            buckets: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}

            # Match by entity + window — UUIDs are from different namespaces
            log_row = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.report_window_start == window_start)
                .where(SQLEntityPipelineRunLog.report_window_end == window_end)
                .where(SQLEntityPipelineRunLog.status == "succeeded")
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).first()

            if not log_row:
                result[run_id_str] = buckets
                continue

            bullet_rows = session.exec(
                select(SQLBulletRunLog)
                .where(SQLBulletRunLog.run_id == log_row.run_id)
                .where(SQLBulletRunLog.is_active == False)  # noqa: E712
            ).all()

            for br in bullet_rows:
                category = _stage_to_category(br.discard_stage)
                if category and br.text:
                    buckets[category].append(br.text)

            result[run_id_str] = buckets

    return result

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


def _run_entities_sequentially(
    *,
    run_ids: list[uuid.UUID],
    entity_ids: list[str],
    pipeline_config: dict,
    state_dir: Path,
    force_run: bool,
    force_window_start,
    force_window_end,
    window_mode,
    rate_limiter: RequestsPerMinuteController,
    connection_sem: Semaphore,
    http_client: httpx.Client,
) -> None:
    """Background task: invoke pipeline for each entity one at a time.

    The three singletons are passed through so that this endpoint shares the
    same 450 QPM budget / connection pool as the parallel endpoint — important
    because operators often mix the two in the same deployment.
    """
    engine = get_engine()
    for run_id, entity_id in zip(run_ids, entity_ids):
        run_entity_incremental(
            run_id=run_id,
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            force_run=force_run,
            force_window_start=force_window_start,
            force_window_end=force_window_end,
            window_mode=window_mode,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
        )


@router.post(
    "/batch/run",
    response_model=BatchRunResponse,
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
    summary="Run pipeline sequentially for multiple entities",
    description=(
        "Submits a list of entities to the pipeline and processes them **one at a time**, "
        "in order. Useful for controlled, lower-concurrency runs or when you want to avoid "
        "saturating the Bigdata API budget.\n\n"
        "**Date window** — omit `force_window_start` / `force_window_end` to use the automatic "
        "incremental window (see `window_mode`). Pass explicit ISO 8601 dates to target a specific "
        "period. One day at a time is recommended for best results.\n\n"
        "**Overlap protection** — if the requested window overlaps an already-completed run for "
        "the same entity, that entity is rejected immediately with an error."
    ),
)
def batch_run(
    body: BatchRunRequest,
    background_tasks: BackgroundTasks,
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
) -> BatchRunResponse:
    if not body.entity_ids:
        return BatchRunResponse(submitted=[], total=0)

    _assert_no_running_entities(body.entity_ids)

    cfg_path = resolve_config_path(None)
    pipeline_config = load_pipeline_config_dict(cfg_path)
    if body.categories:
        pipeline_config["categories"] = body.categories
    state_dir = _resolve_state_dir(None)
    run_ids = [uuid.uuid4() for _ in body.entity_ids]

    background_tasks.add_task(
        _run_entities_sequentially,
        run_ids=run_ids,
        entity_ids=body.entity_ids,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        force_run=False,
        force_window_start=body.force_window_start,
        force_window_end=body.force_window_end,
        window_mode=body.window_mode,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )

    submitted = [
        RunSubmittedResponse(run_id=str(rid), entity_id=eid)
        for rid, eid in zip(run_ids, body.entity_ids)
    ]
    return BatchRunResponse(submitted=submitted, total=len(submitted))


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
        "- `daily` *(default)* — covers `[UTC midnight of today → now]`. If the pipeline already "
        "ran today it resumes from where that run ended; if the last run was yesterday or earlier "
        "it always resets to midnight of today.\n"
        "- `continuous` — covers `[end of last run → now]`, picking up exactly where the previous "
        "run stopped regardless of which day it was. Falls back to `[UTC midnight of today → now]` "
        "if no previous run exists. Use this mode to guarantee no gaps across consecutive runs.\n\n"
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
        entity_ids = _UNIVERSES.get(body.universe)
        if entity_ids is None:
            raise HTTPException(
                status_code=404,
                detail=f"Universe '{body.universe}' not found. Available: {list(_UNIVERSES)}",
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
                # After all entities finish, generate the portfolio brief in background
                import threading as _threading
                _ranking_metric = body.ranking_metric
                def _gen_portfolio_brief():
                    try:
                        from sqlmodel import Session as _Session, select as _select
                        from sqlalchemy import desc as _desc
                        from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog as _RunLog
                        from bigdata_briefs.orchestration.portfolio_brief import generate_and_store_portfolio_brief
                        with _Session(engine) as _s:
                            latest = _s.exec(
                                _select(_RunLog)
                                .where(_RunLog.run_id.in_(run_ids))
                                .where(_RunLog.status.in_(["succeeded", "no_data"]))
                                .order_by(_desc(_RunLog.report_window_end))
                            ).first()
                        if latest and latest.report_window_end:
                            date_iso = latest.report_window_end.date().isoformat()
                            generate_and_store_portfolio_brief(
                                engine, date_iso, top_n=5,
                                ranking_metric=_ranking_metric,
                            )
                    except Exception:
                        logger.exception("Portfolio brief post-batch trigger failed")
                _threading.Thread(target=_gen_portfolio_brief, daemon=True).start()

                # Also store signal history for all entities after the batch completes
                def _store_signals():
                    try:
                        from bigdata_briefs.orchestration.sentiment_ranking import compute_and_store_signals
                        compute_and_store_signals(engine, entity_ids)
                    except Exception:
                        logger.exception("Signal history post-batch trigger failed")
                _threading.Thread(target=_store_signals, daemon=True).start()

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


@router.post(
    "/batch/status",
    response_model=BatchStatusResponse,
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
    summary="Health-check on a batch run",
    description=(
        "Given the list of `run_id`s returned by **POST /api/v1/batch/run**, "
        "returns how many runs have succeeded, are still running, or failed. "
        "Runs whose ID is not yet in the DB are counted as `running` "
        "(the background worker hasn't started them yet)."
    ),
)
def batch_status(body: BatchStatusRequest) -> BatchStatusResponse:
    items: list[BatchRunStatusItem] = []
    succeeded = failed = running = not_found = 0

    with Session(get_engine()) as session:
        for run_id_str in body.run_ids:
            try:
                run_uuid = uuid.UUID(run_id_str)
            except ValueError:
                not_found += 1
                items.append(BatchRunStatusItem(
                    run_id=run_id_str,
                    status="not_found",
                    error_message="Invalid UUID format",
                ))
                continue

            row = session.get(SQLEntityPipelineRunLog, run_uuid)

            if row is None:
                # Not yet written — the background task hasn't started it yet
                running += 1
                items.append(BatchRunStatusItem(
                    run_id=run_id_str,
                    status="running",
                ))
                continue

            error_message: str | None = None
            if row.error_summary:
                error_message = row.error_summary.split("\n\n", 1)[0]

            if row.status == "succeeded":
                succeeded += 1
            elif row.status == "failed":
                failed += 1
            else:
                running += 1

            items.append(BatchRunStatusItem(
                run_id=run_id_str,
                entity_id=row.entity_id,
                status=row.status,
                error_message=error_message,
                started_at=row.process_started_at_utc,
                completed_at=row.process_completed_at_utc,
            ))

    return BatchStatusResponse(
        total=len(body.run_ids),
        succeeded=succeeded,
        failed=failed,
        running=running,
        not_found=not_found,
        runs=items,
    )


def _build_entity_result_from_run_log(
    entity_id: str,
    engine: "Engine",
) -> EntityBulletsResult:
    """
    Build an EntityBulletsResult for an entity whose bullets were all discarded
    (i.e. nothing was saved to SQLiteGeneratedBulletPointStorage).

    Falls back to SQLEntityPipelineRunLog.output_json to recover discarded bullet
    texts.  Entity name is taken from SQLEntityOrchestrationState.kg_name.

    Returns ``found=False`` when no succeeded run row exists for the entity.
    """
    with Session(engine) as session:
        # Entity name from KG orchestration state
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        entity_name: str | None = orch.kg_name if orch else None

        # All succeeded runs for this entity, newest first
        run_rows = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).all()

    if not run_rows:
        return EntityBulletsResult(entity_id=entity_id, found=False)

    runs: list[RunBulletsResult] = []
    for row in run_rows:
        buckets: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}

        with Session(engine) as session:
            bullet_rows = session.exec(
                select(SQLBulletRunLog)
                .where(SQLBulletRunLog.run_id == row.run_id)
                .where(SQLBulletRunLog.is_active == False)  # noqa: E712
            ).all()

        for br in bullet_rows:
            category = _stage_to_category(br.discard_stage)
            if category and br.text:
                buckets[category].append(br.text)

        bullets_discarded = sum(len(v) for v in buckets.values())
        run_created_at = row.process_completed_at_utc or row.process_started_at_utc
        runs.append(
            RunBulletsResult(
                run_id=str(row.run_id),
                report_window_start=row.report_window_start,
                report_window_end=row.report_window_end,
                run_created_at=run_created_at,
                bullet_count=0,
                bullets_saved=0,
                bullets_discarded=bullets_discarded,
                bullets=[],
                discarded_by_relevance=buckets["relevance"],
                discarded_by_grounding=buckets["grounding"],
                discarded_by_novelty=buckets["novelty"],
            )
        )

    return EntityBulletsResult(
        entity_id=entity_id,
        found=True,
        entity_name=entity_name,
        total_runs=len(runs),
        total_bullets=0,
        runs=runs,
    )


@router.post(
    "/batch/bullets",
    response_model=BatchBulletsResponse,
    dependencies=[Depends(require_api_key)],
    summary="Get published bullets for multiple entities, grouped by run",
    description=(
        "Returns the published bullet points for one or more entities, grouped by run and ordered "
        "newest-first. Pass an empty `entity_ids` list to retrieve all entities in the database.\n\n"
        "Each bullet includes the final text, source citations (headline, chunk text, date), and "
        "novelty metadata (`search_action`, `not_fully_novel`). Amber bullets — partially novel, "
        "rewritten to surface only the new element — are flagged with `not_fully_novel: true`."
    ),
)
def batch_bullets(body: BatchBulletsRequest) -> BatchBulletsResponse:
    engine = get_engine()
    storage = SQLiteGeneratedBulletPointStorage(engine)
    results: list[EntityBulletsResult] = []
    entity_ids = body.entity_ids or _all_entity_ids(engine)

    for entity_id in entity_ids:
        grouped = storage.get_all_runs_bullets(entity_id)

        if not grouped:
            # No saved bullets — entity may have run but had everything discarded.
            # Fall back to the run log to surface the entity + discarded lists.
            results.append(_build_entity_result_from_run_log(entity_id, engine))
            continue

        # Build {run_id -> (entity_id, window_start, window_end)} for the lookup.
        # SQLGeneratedBulletPoint.run_id != SQLEntityPipelineRunLog.run_id, so we
        # match the log rows by entity + window instead of by UUID.
        run_info: dict[str, tuple[str, datetime, datetime]] = {
            run_id: (rows[0].entity_id, rows[0].report_window_start, rows[0].report_window_end)
            for run_id, rows in grouped.items()
        }
        discarded_map = _load_discarded_for_runs(run_info)

        entity_name: str | None = None
        runs: list[RunBulletsResult] = []

        for run_id, rows in grouped.items():
            first = rows[0]
            if entity_name is None:
                entity_name = first.entity_name

            bullets = [
                BulletPointItem(
                    trace_id=row.trace_id,
                    text=row.text,
                    citations=[
                        CitationDetail(
                            id=c["id"],
                            headline=c["headline"],
                            text=c["text"],
                        )
                        for c in (row.citations or [])
                    ],
                    embedding_decision=row.embedding_decision,
                    search_action=row.search_action,
                    not_fully_novel=row.not_fully_novel or False,
                )
                for row in rows
            ]

            discarded = discarded_map.get(run_id, {})
            discarded_relevance = discarded.get("relevance", [])
            discarded_grounding = discarded.get("grounding", [])
            discarded_novelty = discarded.get("novelty", [])
            bullets_discarded = (
                len(discarded_relevance) + len(discarded_grounding) + len(discarded_novelty)
            )
            runs.append(
                RunBulletsResult(
                    run_id=run_id,
                    report_window_start=first.report_window_start,
                    report_window_end=first.report_window_end,
                    run_created_at=first.created_at,
                    bullet_count=len(bullets),
                    bullets_saved=len(bullets),
                    bullets_discarded=bullets_discarded,
                    bullets=bullets,
                    discarded_by_relevance=discarded_relevance,
                    discarded_by_grounding=discarded_grounding,
                    discarded_by_novelty=discarded_novelty,
                )
            )

        total_bullets = sum(r.bullet_count for r in runs)
        results.append(
            EntityBulletsResult(
                entity_id=entity_id,
                found=True,
                entity_name=entity_name,
                total_runs=len(runs),
                total_bullets=total_bullets,
                runs=runs,
            )
        )

    return BatchBulletsResponse(
        results=results,
        total_entities=len(results),
        total_bullets=sum(r.total_bullets for r in results),
    )


# ── Batch bullets detail ──────────────────────────────────────────────────────


def _resolve_citations(
    citation_ids: list[str],
    source_lookup: dict[str, dict],
) -> list[CitationDetail] | None:
    if not citation_ids:
        return None
    return [
        CitationDetail(
            id=cid,
            headline=(source_lookup.get(cid) or {}).get("headline", ""),
            text=(source_lookup.get(cid) or {}).get("text", ""),
            source_name=(source_lookup.get(cid) or {}).get("source_name", ""),
        )
        for cid in citation_ids
    ] or None


def _build_bullet_detail(
    bp: dict,
    cite_map: dict[str, list[CitationDetail]] | None = None,
    source_lookup: dict[str, dict] | None = None,
) -> BulletDetailItem:
    """Convert a raw BulletPointRecord dict into a BulletDetailItem with full reasoning."""
    is_active = bp.get("is_active", True)
    generation = bp.get("generation") or {}
    original_text = generation.get("original_text") or bp.get("text", "")
    final_text = bp.get("text", "")
    rewritten = final_text != original_text

    if is_active:
        rs = bp.get("relevance_scoring") or {}
        passed = BulletPassedDetail(
            relevance_score=rs.get("score", 0),
            relevance_reason=rs.get("reason", ""),
        ) if rs else None
        trace_id = bp.get("trace_id", "")
        citations = (cite_map or {}).get(trace_id) or None
        return BulletDetailItem(
            trace_id=trace_id,
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=True,
            citations=citations,
            passed=passed,
        )

    # For discarded bullets, resolve citations from source_references if available
    discarded_citations = _resolve_citations(
        bp.get("citations") or [], source_lookup or {}
    )

    # --- Discarded bullet: find which stage eliminated it ---

    # 1. Initial relevance score
    rs = bp.get("relevance_scoring") or {}
    if rs and not rs.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="relevance_score",
                reason=rs.get("reason", ""),
                score=rs.get("score"),
            ),
        )

    # 2. Entity grounding
    eg_check = (bp.get("entity_grounding") or {}).get("check") or {}
    if eg_check.get("decision") == "invalid":
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="grounding",
                reason=eg_check.get("reason", ""),
            ),
        )

    # 3. Novelty embedding — judgment
    ne = bp.get("novelty_embedding") or {}
    judgment = ne.get("judgment") or {}
    if judgment.get("decision") == "discard":
        _EMBEDDING_STRIP_KEYS = {"evidence_ids", "evidence"}
        clean_evaluators = []
        for ev in (judgment.get("evaluator_details") or []):
            ev_clean = {k: v for k, v in ev.items() if k not in _EMBEDDING_STRIP_KEYS}
            if "retrieved_bullets" in ev_clean:
                ev_clean["retrieved_bullets"] = [
                    {k: v for k, v in rb.items() if k not in _EMBEDDING_STRIP_KEYS}
                    for rb in (ev_clean["retrieved_bullets"] or [])
                ]
            clean_evaluators.append(ev_clean)
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_embedding",
                reason=judgment.get("reason", ""),
                evaluator_details=clean_evaluators,
            ),
        )

    # 4. Novelty embedding — relevance check on rewritten bullet
    emb_rc = ne.get("relevance_check") or {}
    if emb_rc and not emb_rc.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_embedding_relevance",
                reason=f"Rewritten bullet scored {emb_rc.get('score')} — below relevance threshold.",
                score=emb_rc.get("score"),
            ),
        )

    # 5. Novelty search — verdict
    ns = bp.get("novelty_search") or {}
    search = ns.get("search") or {}
    if search.get("verdict") == "discard":
        details = search.get("details") or {}
        raw_verdicts = details.get("claim_verdicts") or []
        claims = details.get("claims") or []
        evidence_map: dict = details.get("evidence_map") or {}
        claim_details: list[ClaimVerdictDetail] = []
        for cv in raw_verdicts:
            idx = cv.get("claim_index", 0)
            claim_text = claims[idx].get("text", "") if idx < len(claims) else ""
            evidence = [
                EvidenceDetail(
                    simple_id=eid,
                    original_doc_id=(evidence_map.get(eid) or {}).get("original_doc_id", ""),
                    chunk_num=(evidence_map.get(eid) or {}).get("chunk_num", 0),
                    headline=(evidence_map.get(eid) or {}).get("headline", ""),
                    date=(evidence_map.get(eid) or {}).get("date", ""),
                    text=(evidence_map.get(eid) or {}).get("text", ""),
                )
                for eid in (cv.get("evidence_ids") or [])
            ]
            claim_details.append(ClaimVerdictDetail(
                claim_index=idx,
                claim_text=claim_text,
                novelty=cv.get("novelty", ""),
                evidence=evidence,
                reasoning=cv.get("reasoning", ""),
            ))
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_search",
                reason=search.get("reason") or "",
                claim_verdicts=claim_details or None,
                overall_verdict=search.get("overall_verdict"),
            ),
        )

    # 6. Novelty search — relevance check on rewritten bullet
    search_rc = ns.get("relevance_check") or {}
    if search_rc and not search_rc.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_search_relevance",
                reason=f"Search-rewritten bullet scored {search_rc.get('score')} — below relevance threshold.",
                score=search_rc.get("score"),
                evaluator_reasoning=search_rc.get("reasoning"),
            ),
        )

    # 7. Node failure
    failure = bp.get("failure") or {}
    return BulletDetailItem(
        trace_id=bp.get("trace_id", ""),
        theme=bp.get("theme", ""),
        original_text=original_text,
        final_text=bp.get("text", ""),
        is_active=False,
        discarded=BulletDiscardDetail(
            stage="error",
            reason=failure.get("error_message", "Unknown pipeline error"),
        ),
    )


@router.post(
    "/batch/bullets/detail",
    response_model=BatchBulletsDetailResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_api_key)],
    summary="Full pipeline detail for multiple entities",
    description=(
        "Returns full pipeline detail for every bullet — both published and discarded — "
        "for one or more entities. Pass an empty `entity_ids` list to retrieve all entities.\n\n"
        "**Published bullets** include the relevance score and reasoning that justified publishing.\n\n"
        "**Discarded bullets** include the stage that eliminated them and the reason:\n"
        "- `relevance_score` — scored too low on financial materiality\n"
        "- `grounding` — text not verifiable against cited sources\n"
        "- `novelty_embedding` — already reported in a previous run\n"
        "- `novelty_search` — per-claim verdicts with the evidence chunks that already covered the information"
    ),
)
def batch_bullets_detail(body: BatchBulletsDetailRequest) -> BatchBulletsDetailResponse:
    engine = get_engine()
    results: list[EntityDetailResult] = []
    entity_ids = body.entity_ids or _all_entity_ids(engine)

    for entity_id in entity_ids:
        with Session(engine) as session:
            orch = session.get(SQLEntityOrchestrationState, entity_id)
            entity_name: str | None = orch.kg_name if orch else None

            query = (
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.status == "succeeded")
            )
            if body.from_date is not None:
                query = query.where(SQLEntityPipelineRunLog.report_window_end >= body.from_date)
            if body.to_date is not None:
                query = query.where(SQLEntityPipelineRunLog.report_window_start <= body.to_date)
            run_rows = session.exec(
                query.order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).all()

        if not run_rows:
            results.append(EntityDetailResult(entity_id=entity_id, found=False))
            continue

        entity_runs: list[RunDetailResult] = []
        for row in run_rows:
            raw_bullets: list[dict] = []
            raw_source_refs: dict = {}
            if row.output_json:
                try:
                    parsed = json.loads(row.output_json)
                    if isinstance(parsed, list):
                        raw_bullets = parsed
                    else:
                        raw_bullets = parsed.get("bullet_points") or []
                        raw_source_refs = parsed.get("source_references") or {}
                except (json.JSONDecodeError, TypeError):
                    pass

            # Build citation_id → {headline, text} lookup from source_references
            source_lookup: dict[str, dict] = {}
            for src in raw_source_refs.values():
                if isinstance(src, dict):
                    doc_id = src.get("document_id")
                    chunk_id = src.get("chunk_id")
                    if doc_id is not None and chunk_id is not None:
                        source_lookup[f"CQS:{doc_id}-{chunk_id}"] = src

            active_trace_ids = [
                bp.get("trace_id") for bp in raw_bullets
                if bp.get("is_active") and bp.get("trace_id")
            ]
            cite_map: dict[str, list[CitationDetail]] = {}
            if active_trace_ids:
                with Session(engine) as session:
                    cite_rows = session.exec(
                        select(SQLGeneratedBulletPoint).where(
                            SQLGeneratedBulletPoint.trace_id.in_(active_trace_ids)
                        )
                    ).all()
                cite_map = {
                    r.trace_id: [
                        CitationDetail(id=c["id"], headline=c["headline"], text=c["text"])
                        for c in (r.citations or [])
                        if isinstance(c, dict)
                    ]
                    for r in cite_rows
                    if r.trace_id
                }

            bullets = [_build_bullet_detail(bp, cite_map, source_lookup) for bp in raw_bullets]
            active = sum(1 for b in bullets if b.is_active)
            entity_runs.append(RunDetailResult(
                run_id=str(row.run_id),
                report_window_start=row.report_window_start,
                report_window_end=row.report_window_end,
                total_bullets=len(bullets),
                active_bullets=active,
                discarded_bullets=len(bullets) - active,
                bullets=bullets,
            ))

        results.append(EntityDetailResult(
            entity_id=entity_id,
            found=True,
            entity_name=entity_name,
            runs=entity_runs,
        ))

    return BatchBulletsDetailResponse(
        results=results,
        total_entities=len(results),
    )

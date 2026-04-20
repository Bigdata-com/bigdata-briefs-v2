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
from threading import Semaphore

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends
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
    BatchBulletsRequest,
    BatchBulletsResponse,
    BatchRunRequest,
    BatchRunResponse,
    BatchRunStatusItem,
    BatchStatusRequest,
    BatchStatusResponse,
    BulletPointItem,
    CitationDetail,
    EntityBulletsResult,
    RunBulletsResult,
    RunSubmittedResponse,
)
from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import SQLEntityOrchestrationState, SQLEntityPipelineRunLog
from bigdata_briefs.settings import settings


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
    (entity_id, report_window_start, report_window_end) and parse its output_json
    to return discarded bullets grouped by category.

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
            row = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.report_window_start == window_start)
                .where(SQLEntityPipelineRunLog.report_window_end == window_end)
                .where(SQLEntityPipelineRunLog.status == "succeeded")
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).first()

            if not row or not row.output_json:
                result[run_id_str] = buckets
                continue

            try:
                bullet_points: list[dict] = json.loads(row.output_json)
            except (json.JSONDecodeError, TypeError):
                result[run_id_str] = buckets
                continue

            for bp in bullet_points:
                category = _classify_discarded(bp)
                if category:
                    text = bp.get("text", "")
                    if text:
                        buckets[category].append(text)

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
    summary="Run pipeline sequentially for multiple entities",
    description=(
        "Submits a single background job that processes each entity **in order**, "
        "one after the other. Returns one `run_id` per entity immediately; "
        "poll **GET /api/v1/runs/{run_id}** to track each run's progress.\n\n"
        "Use `force_window_start` / `force_window_end` (ISO 8601) to fix the report "
        "window for all entities in the batch — useful for backfilling a specific day."
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

    cfg_path = resolve_config_path(None)
    pipeline_config = (
        body.pipeline_config
        if body.pipeline_config is not None
        else load_pipeline_config_dict(cfg_path)
    )
    state_dir = _resolve_state_dir(body.state_dir)
    run_ids = [uuid.uuid4() for _ in body.entity_ids]

    background_tasks.add_task(
        _run_entities_sequentially,
        run_ids=run_ids,
        entity_ids=body.entity_ids,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        force_run=body.force_run,
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

    ``startup_delay_seconds`` staggers concurrent entity starts: each worker
    sleeps this long before beginning so that API connections (Bigdata, OpenAI)
    are not all opened at the exact same instant.
    """
    if startup_delay_seconds > 0:
        import time as _time
        logger.info(
            "Entity worker stagger delay",
            entity_id=entity_id,
            startup_delay_s=startup_delay_seconds,
        )
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
    response_model=BatchRunResponse,
    dependencies=[Depends(require_api_key)],
    summary="Run pipeline in parallel for multiple entities",
    description=(
        "Submits each entity to a process-wide worker pool (``MAX_CONCURRENT_ENTITIES``). "
        "Returns one ``run_id`` per entity immediately; poll "
        "**GET /api/v1/runs/{run_id}** to track each run.\n\n"
        "All concurrent runs share one 450 QPM Bigdata budget and one connection pool, "
        "so this endpoint is safe to call with many entities at once. "
        "Use **GET /api/v1/rate/status** to observe the current budget usage."
    ),
)
def batch_run_parallel(
    body: BatchRunRequest,
    executor: ThreadPoolExecutor = Depends(get_entity_executor),
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
) -> BatchRunResponse:
    if not body.entity_ids:
        return BatchRunResponse(submitted=[], total=0)

    cfg_path = resolve_config_path(None)
    pipeline_config = (
        body.pipeline_config
        if body.pipeline_config is not None
        else load_pipeline_config_dict(cfg_path)
    )
    state_dir = _resolve_state_dir(body.state_dir)
    run_ids = [uuid.uuid4() for _ in body.entity_ids]
    engine = get_engine()

    # Stagger entity starts: each worker sleeps 3 s × its position index before
    # beginning.  This prevents all worker threads from opening Bigdata / OpenAI
    # connections at the exact same instant, which caused burst connection errors.
    _ENTITY_STAGGER_SECONDS = 3.0
    for idx, (run_id, entity_id) in enumerate(zip(run_ids, body.entity_ids)):
        executor.submit(
            _run_one_entity_safely,
            run_id=run_id,
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            force_run=body.force_run,
            force_window_start=body.force_window_start,
            force_window_end=body.force_window_end,
            window_mode=body.window_mode,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
            startup_delay_seconds=idx * _ENTITY_STAGGER_SECONDS,
        )

    submitted = [
        RunSubmittedResponse(run_id=str(rid), entity_id=eid)
        for rid, eid in zip(run_ids, body.entity_ids)
    ]
    return BatchRunResponse(submitted=submitted, total=len(submitted))


@router.post(
    "/batch/status",
    response_model=BatchStatusResponse,
    dependencies=[Depends(require_api_key)],
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
        if row.output_json:
            try:
                for bp in json.loads(row.output_json):
                    category = _classify_discarded(bp)
                    if category:
                        text = bp.get("text", "")
                        if text:
                            buckets[category].append(text)
            except (json.JSONDecodeError, TypeError):
                pass

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
    summary="Get all bullets for multiple entities, grouped by run",
    description=(
        "Returns **every** bullet point stored for each entity, organised into "
        "separate run blocks ordered newest-first. "
        "Entities with no data are included with `found=false`."
    ),
)
def batch_bullets(body: BatchBulletsRequest) -> BatchBulletsResponse:
    engine = get_engine()
    storage = SQLiteGeneratedBulletPointStorage(engine)
    results: list[EntityBulletsResult] = []

    for entity_id in body.entity_ids:
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

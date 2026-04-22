"""
Routes: run lifecycle

    POST /api/v1/entities/{entity_id}/run        → trigger async pipeline run
    GET  /api/v1/runs/{run_id}                   → fetch run status / metadata
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Semaphore

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_engine,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.api.schemas import (
    BulletTrace,
    DateRangeRunRequest,
    DateRangeRunResponse,
    DateRangeRunSubmittedItem,
    EmbeddingJudgmentTrace,
    EmbeddingTrace,
    GroundingTrace,
    RelevanceScoringTrace,
    RunRequest,
    RunStatusResponse,
    RunSubmittedResponse,
    RunTraceResponse,
    SearchTrace,
)
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog
from bigdata_briefs.settings import settings


def _parse_bullet_trace(bp: dict) -> BulletTrace:
    """Convert a raw BulletPointRecord dict (from output_json) into a BulletTrace."""

    # relevance_scoring
    rs_raw = bp.get("relevance_scoring") or {}
    relevance_scoring = (
        RelevanceScoringTrace(
            score=rs_raw.get("score", 0),
            reason=rs_raw.get("reason", ""),
            passed=rs_raw.get("passed", False),
        )
        if rs_raw
        else None
    )

    # entity_grounding
    eg_raw = (bp.get("entity_grounding") or {}).get("check") or {}
    grounding = (
        GroundingTrace(
            decision=eg_raw.get("decision", ""),
            reason=eg_raw.get("reason", ""),
        )
        if eg_raw
        else None
    )

    # novelty_embedding
    ne_raw = bp.get("novelty_embedding") or {}
    j_raw = ne_raw.get("judgment") or {}
    rew_raw = ne_raw.get("rewrite") or {}
    rel_raw = ne_raw.get("relevance_check") or {}
    embedding = (
        EmbeddingTrace(
            judgment=EmbeddingJudgmentTrace(
                decision=j_raw.get("decision", ""),
                reason=j_raw.get("reason", ""),
                evaluator_details=j_raw.get("evaluator_details") or [],
            ) if j_raw else None,
            rewritten_text=rew_raw.get("text_after") if rew_raw else None,
            relevance_score=rel_raw.get("score") if rel_raw else None,
            relevance_passed=rel_raw.get("passed") if rel_raw else None,
        )
        if ne_raw
        else None
    )

    # novelty_search
    ns_raw = bp.get("novelty_search") or {}
    s_raw = ns_raw.get("search") or {}
    sr_raw = ns_raw.get("relevance_check") or {}
    search = (
        SearchTrace(
            verdict=s_raw.get("verdict", ""),
            rewritten_text=s_raw.get("rewritten_text"),
            duration_seconds=s_raw.get("duration_seconds"),
            reason=s_raw.get("reason"),
            details=s_raw.get("details"),
            relevance_score=sr_raw.get("score") if sr_raw else None,
            relevance_passed=sr_raw.get("passed") if sr_raw else None,
        )
        if s_raw
        else None
    )

    return BulletTrace(
        trace_id=bp.get("trace_id", ""),
        is_active=bp.get("is_active", True),
        theme=bp.get("theme", ""),
        text=bp.get("text", ""),
        citations=bp.get("citations") or [],
        relevance_scoring=relevance_scoring,
        grounding=grounding,
        embedding=embedding,
        search=search,
        failure=bp.get("failure"),
    )

router = APIRouter(tags=["runs"])


def _resolve_state_dir(state_dir: str | None) -> Path:
    if state_dir:
        return Path(state_dir).expanduser()
    env = settings.BRIEF_PIPELINE_STATE_DIR.strip()
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".brief_pipeline_state"


def _run_pipeline_background(
    *,
    run_id: uuid.UUID,
    entity_id: str,
    pipeline_config: dict,
    state_dir: Path,
    refresh_entity: bool,
    force_run: bool,
    force_window_start,
    force_window_end,
    window_mode,
    rate_limiter: RequestsPerMinuteController,
    connection_sem: Semaphore,
    http_client: httpx.Client,
) -> None:
    """Sync function executed in a thread pool by FastAPI BackgroundTasks.

    The shared rate limiter / connection semaphore / http client are injected
    from ``app.state`` so a single-entity trigger shares the same 450 QPM
    budget as concurrent runs from other endpoints.
    """
    run_entity_incremental(
        run_id=run_id,
        entity_id=entity_id,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        refresh_entity=refresh_entity,
        force_run=force_run,
        force_window_start=force_window_start,
        force_window_end=force_window_end,
        window_mode=window_mode,
        engine=get_engine(),
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )


@router.post(
    "/entities/{entity_id}/run",
    response_model=RunSubmittedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
    summary="Trigger an incremental pipeline run",
    description=(
        "Submits an incremental pipeline run for the given entity and returns immediately. "
        "Poll **GET /api/v1/runs/{run_id}** to check progress. "
        "Note: the run row may not exist in the database for a brief moment after submission "
        "while the background worker starts — retry on 404."
    ),
)
async def trigger_run(
    entity_id: str,
    body: RunRequest,
    background_tasks: BackgroundTasks,
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
) -> RunSubmittedResponse:
    run_id = uuid.uuid4()

    cfg_path = resolve_config_path(None)
    pipeline_config = (
        body.pipeline_config
        if body.pipeline_config is not None
        else load_pipeline_config_dict(cfg_path)
    )
    state_dir = _resolve_state_dir(body.state_dir)

    background_tasks.add_task(
        _run_pipeline_background,
        run_id=run_id,
        entity_id=entity_id,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        refresh_entity=body.refresh_entity,
        force_run=body.force_run,
        force_window_start=body.force_window_start,
        force_window_end=body.force_window_end,
        window_mode=body.window_mode,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )

    return RunSubmittedResponse(run_id=str(run_id), entity_id=entity_id)


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
        # error_summary stores "short message\n\ntraceback..." when an exception was caught
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


@router.get(
    "/runs/{run_id}/trace",
    response_model=RunTraceResponse,
    dependencies=[Depends(require_api_key)],
    summary="Full per-bullet step trace for a run",
    description=(
        "Returns the complete pipeline trace for every bullet processed in a run: "
        "relevance score, grounding decision, embedding novelty judgment, "
        "search novelty verdict, and post-search relevance check — "
        "including bullets that were discarded along the way (`is_active=false`).\n\n"
        "Available only after the run has completed (status `succeeded` or `failed`). "
        "Returns 404 if the run does not exist, 409 if it is still running, "
        "and 204 if it completed but produced no bullet trace (e.g. early exit)."
    ),
)
def get_run_trace(run_id: uuid.UUID) -> RunTraceResponse:
    """Return the full per-bullet step trace saved at the end of the run."""
    with Session(get_engine()) as session:
        row = session.get(SQLEntityPipelineRunLog, run_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found.",
        )
    if row.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is still in progress.",
        )
    if not row.output_json:
        raise HTTPException(
            status_code=status.HTTP_204_NO_CONTENT,
            detail=f"Run {run_id} completed but has no bullet trace (early exit or no bullets).",
        )

    try:
        raw_bullets: list[dict] = json.loads(row.output_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Trace data for run {run_id} is malformed: {exc}",
        )

    bullets = [_parse_bullet_trace(bp) for bp in raw_bullets]

    return RunTraceResponse(
        run_id=str(row.run_id),
        entity_id=row.entity_id,
        total_bullets=len(bullets),
        active_bullets=sum(1 for b in bullets if b.is_active),
        bullets=bullets,
    )


def _run_entity_date_range(
    *,
    entity_id: str,
    day_runs: list[tuple[date, uuid.UUID]],
    pipeline_config: dict,
    state_dir: Path,
    refresh_entity: bool,
    force_run: bool,
    window_mode,
    rate_limiter: RequestsPerMinuteController,
    connection_sem: Semaphore,
    http_client: httpx.Client,
) -> None:
    """Background task: run the pipeline once per day, sequentially."""
    engine = get_engine()
    for day, run_id in day_runs:
        window_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
        window_end = window_start + timedelta(days=1)
        run_entity_incremental(
            run_id=run_id,
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            refresh_entity=refresh_entity,
            force_run=force_run,
            force_window_start=window_start,
            force_window_end=window_end,
            window_mode=window_mode,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
        )


@router.post(
    "/entities/{entity_id}/run-range",
    response_model=DateRangeRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
    summary="Run pipeline day-by-day over a date range",
    description=(
        "Submits one pipeline run per day from `start_date` to `end_date` (inclusive), "
        "processed sequentially in chronological order. "
        "Returns one `run_id` per day immediately; "
        "poll **GET /api/v1/runs/{run_id}** to track each run's progress.\n\n"
        "Each day's window is midnight-to-midnight UTC "
        "(`YYYY-MM-DDT00:00:00Z` → `YYYY-MM-DDT00:00:00Z` next day)."
    ),
)
async def run_date_range(
    entity_id: str,
    body: DateRangeRunRequest,
    background_tasks: BackgroundTasks,
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
) -> DateRangeRunResponse:
    if body.end_date < body.start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be >= start_date",
        )

    days: list[date] = []
    current = body.start_date
    while current <= body.end_date:
        days.append(current)
        current += timedelta(days=1)

    day_runs = [(d, uuid.uuid4()) for d in days]

    cfg_path = resolve_config_path(None)
    pipeline_config = (
        body.pipeline_config
        if body.pipeline_config is not None
        else load_pipeline_config_dict(cfg_path)
    )
    state_dir = _resolve_state_dir(body.state_dir)

    background_tasks.add_task(
        _run_entity_date_range,
        entity_id=entity_id,
        day_runs=day_runs,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        refresh_entity=body.refresh_entity,
        force_run=body.force_run,
        window_mode=body.window_mode,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )

    return DateRangeRunResponse(
        entity_id=entity_id,
        total_days=len(days),
        submitted=[
            DateRangeRunSubmittedItem(date=str(d), run_id=str(run_id), entity_id=entity_id)
            for d, run_id in day_runs
        ],
    )

"""
Historical day-by-day scan for a single entity.

REST:
    POST /api/v1/scan        → start a scan, returns scan_id
    GET  /api/v1/scan/{id}   → scan status + results
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import desc
from sqlmodel import Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_engine,
    get_entity_executor,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLUIScanRun,
)
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController

from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore
import httpx

from bigdata_briefs import logger

router = APIRouter(tags=["scan"])


# ── Window generation ─────────────────────────────────────────────────────────


def _ensure_utc_scan(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_process_completed_at_utc(engine, entity_id: str) -> datetime | None:
    """Latest succeeded/no_data run completion timestamp — fallback for old-style DB records."""
    with Session(engine) as session:
        row = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
            .where(SQLEntityPipelineRunLog.process_completed_at_utc.isnot(None))
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            .limit(1)
        ).first()
    if row is None or row.process_completed_at_utc is None:
        return None
    return _ensure_utc_scan(row.process_completed_at_utc)


def build_scan_windows(
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Return a list of (window_start, window_end) covering start→end day by day.

    Each window spans one UTC calendar day:
      - window_start: start of the day (or ``start`` for the first day)
      - window_end:   23:59:59 of the day, clipped by ``end``

    Pass ``end = datetime.now(timezone.utc)`` for a live scan: the last window will
    naturally stop at ``now`` rather than end-of-day.
    """
    windows: list[tuple[datetime, datetime]] = []
    cursor = _ensure_utc_scan(start)  # keep sub-second precision for resume points
    end = _ensure_utc_scan(end)

    while cursor < end:
        day_end = cursor.replace(hour=23, minute=59, second=59, microsecond=0)
        window_end = min(day_end, end)
        windows.append((cursor, window_end))
        cursor = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    return windows


def resolve_scan_start(
    engine,
    entity_id: str,
    requested_start: datetime,
    scan_end: datetime,
) -> datetime:
    """Return the effective first instant for a day-by-day scan.

    Normal case: resume from ``last_window_end`` when it falls in ``(requested_start, scan_end)``.

    Transition case (old DB records stored nominal 23:59:59 as window end):
    when ``last_window_end >= scan_end`` but they share the same UTC calendar day,
    fall back to the latest ``process_completed_at_utc`` if that time is a valid resume
    point, then to ``requested_start`` if still stuck.
    """
    requested_start = _ensure_utc_scan(requested_start)
    scan_end = _ensure_utc_scan(scan_end)
    base = requested_start
    last_resume: datetime | None = None
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        if orch and orch.last_window_end:
            last_resume = _ensure_utc_scan(orch.last_window_end)
            if requested_start < last_resume < scan_end:
                return last_resume
            if last_resume > base:
                base = last_resume
    # Stuck: last_window_end >= scan_end.  If they share the same calendar day the stored
    # end was likely a nominal 23:59:59; use process_completed_at_utc as the real resume
    # point so the scan covers the remaining hours of that day.
    if base >= scan_end and last_resume is not None and last_resume.date() == scan_end.date():
        pc = _latest_process_completed_at_utc(engine, entity_id)
        if pc is not None and requested_start <= pc < scan_end:
            return pc
        return requested_start
    return base


# ── DB helpers ────────────────────────────────────────────────────────────────


def db_create_scan(engine, scan_id: str, entity_id: str, entity_name: str, total: int) -> None:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(SQLUIScanRun(
            scan_id=scan_id,
            entity_id=entity_id,
            entity_name=entity_name,
            status="running",
            windows_total=total,
            windows_done=0,
            results_json="[]",
            created_at=now,
            updated_at=now,
        ))
        session.commit()


def db_append_window_result(engine, scan_id: str, result: dict) -> None:
    with Session(engine) as session:
        row = session.get(SQLUIScanRun, scan_id)
        if row is None:
            return
        existing = json.loads(row.results_json)
        existing.append(result)
        row.results_json = json.dumps(existing)
        row.windows_done += 1
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def db_finish_scan(engine, scan_id: str) -> None:
    with Session(engine) as session:
        row = session.get(SQLUIScanRun, scan_id)
        if row is None:
            return
        row.status = "finished"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def db_cancel_scan(engine, scan_id: str) -> None:
    with Session(engine) as session:
        row = session.get(SQLUIScanRun, scan_id)
        if row is None:
            return
        row.status = "cancelled"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def db_get_scan(engine, scan_id: str) -> SQLUIScanRun | None:
    with Session(engine) as session:
        return session.get(SQLUIScanRun, scan_id)


def db_is_scan_cancelled(engine, scan_id: str) -> bool:
    row = db_get_scan(engine, scan_id)
    return row is not None and row.status == "cancelled"


# ── Worker ────────────────────────────────────────────────────────────────────


def run_scan_worker(
    *,
    scan_id: str,
    entity_id: str,
    windows: list[tuple[datetime, datetime]],
    engine,
    rate_limiter,
    connection_sem,
    http_client,
    source_categories: list[str] | None = None,
) -> None:
    """Sequential worker: runs each daily window in order, writing results to DB."""
    pipeline_config = load_pipeline_config_dict(resolve_config_path(None))
    if source_categories:
        pipeline_config["categories"] = source_categories
    state_dir = Path(".brief_pipeline_state")

    total_windows = len(windows)
    logger.info(
        "scan_worker_start",
        scan_id=scan_id,
        entity_id=entity_id,
        windows=total_windows,
        first_window=windows[0][0].strftime("%Y-%m-%d %H:%M UTC") if windows else None,
        last_window=windows[-1][1].strftime("%Y-%m-%d %H:%M UTC") if windows else None,
    )

    for idx, (window_start, window_end) in enumerate(windows, start=1):
        if db_is_scan_cancelled(engine, scan_id):
            logger.warning(
                "scan_worker_cancelled",
                scan_id=scan_id,
                entity_id=entity_id,
                window=f"{window_start.strftime('%Y-%m-%d %H:%M')} → {window_end.strftime('%H:%M')} UTC",
            )
            db_append_window_result(engine, scan_id, {
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "status": "cancelled",
            })
            continue

        logger.info(
            "scan_window_start",
            scan_id=scan_id,
            entity_id=entity_id,
            window=f"{window_start.strftime('%Y-%m-%d %H:%M')} → {window_end.strftime('%H:%M')} UTC",
            progress=f"{idx}/{total_windows}",
        )
        try:
            result = run_entity_incremental(
                entity_id=entity_id,
                pipeline_config=pipeline_config,
                state_dir=state_dir,
                force_window_start=window_start,
                force_window_end=window_end,
                engine=engine,
                rate_limiter=rate_limiter,
                connection_sem=connection_sem,
                http_client=http_client,
            )
            status = "succeeded" if result.success else "failed"
            logger.info(
                "scan_window_done",
                scan_id=scan_id,
                entity_id=entity_id,
                window=f"{window_start.strftime('%Y-%m-%d %H:%M')} → {window_end.strftime('%H:%M')} UTC",
                status=status,
                run_id=str(result.run_id) if result.run_id else None,
                error=result.error if not result.success else None,
            )
            payload = {
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "status": status,
                "error": result.error if not result.success else None,
            }
            if result.run_id:
                payload["run_id"] = str(result.run_id)
            db_append_window_result(engine, scan_id, payload)
        except Exception as exc:
            logger.exception(
                "scan_window_error",
                scan_id=scan_id,
                entity_id=entity_id,
                window=f"{window_start.strftime('%Y-%m-%d %H:%M')} → {window_end.strftime('%H:%M')} UTC",
            )
            db_append_window_result(engine, scan_id, {
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "status": "failed",
                "error": str(exc),
            })

    logger.info("scan_worker_finished", scan_id=scan_id, entity_id=entity_id)
    db_finish_scan(engine, scan_id)


# ── REST endpoint ─────────────────────────────────────────────────────────────


from fastapi import HTTPException
from pydantic import BaseModel

from bigdata_briefs.api.routes.universes import _UNIVERSES


class ScanRequest(BaseModel):
    entity_id: str | None = None   # required unless universe is set
    universe: str | None = None    # scan all entities in this universe
    start_date: str                # YYYY-MM-DD
    end_date: str | None = None    # YYYY-MM-DD, defaults to today


class ScanResponse(BaseModel):
    scan_id: str
    entity_id: str
    windows_total: int
    start: str
    end: str


class UniverseScanResponse(BaseModel):
    scans: list[ScanResponse]
    total_entities: int
    universe: str


def _parse_dates(start_date: str, end_date: str | None) -> tuple[datetime, datetime]:
    try:
        requested_start = datetime.strptime(start_date, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=timezone.utc
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="start_date must be YYYY-MM-DD")
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD")
    else:
        end = datetime.now(timezone.utc)
    return requested_start, end


def _start_one_scan(
    entity_id: str,
    requested_start: datetime,
    end: datetime,
    engine,
    executor,
    rate_limiter,
    connection_sem,
    http_client,
) -> ScanResponse | None:
    """Create and submit one scan. Returns None if already up to date."""
    effective_start = resolve_scan_start(engine, entity_id, requested_start, end)
    windows = build_scan_windows(effective_start, end)
    if not windows:
        return None

    entity_name = entity_id
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        if orch and orch.kg_name:
            entity_name = orch.kg_name

    scan_id = str(uuid.uuid4())
    db_create_scan(engine, scan_id, entity_id, entity_name, len(windows))
    executor.submit(
        run_scan_worker,
        scan_id=scan_id,
        entity_id=entity_id,
        windows=windows,
        engine=engine,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )
    return ScanResponse(
        scan_id=scan_id,
        entity_id=entity_id,
        windows_total=len(windows),
        start=effective_start.isoformat(),
        end=end.isoformat(),
    )


@router.post(
    "/scan",
    dependencies=[Depends(require_api_key)],
    summary="Start a historical day-by-day scan for one entity or an entire universe",
    description=(
        "Provide either `entity_id` (single entity) or `universe` (all entities in a "
        "named universe). Runs the pipeline once per calendar day from `start_date` to "
        "`end_date` (default: today). Resumes from the last completed window automatically.\n\n"
        "Single entity → returns a `ScanResponse` with one `scan_id`.\n"
        "Universe → returns a `UniverseScanResponse` with one `scan_id` per entity."
    ),
)
def start_scan(
    body: ScanRequest,
    executor: ThreadPoolExecutor = Depends(get_entity_executor),
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    http_client: httpx.Client = Depends(get_http_client),
):
    if not body.entity_id and not body.universe:
        raise HTTPException(status_code=422, detail="Provide either entity_id or universe.")
    if body.entity_id and body.universe:
        raise HTTPException(status_code=422, detail="Provide either entity_id or universe, not both.")

    engine = get_engine()
    requested_start, end = _parse_dates(body.start_date, body.end_date)

    if body.universe:
        entity_ids = _UNIVERSES.get(body.universe)
        if entity_ids is None:
            raise HTTPException(
                status_code=404,
                detail=f"Universe '{body.universe}' not found. Available: {list(_UNIVERSES)}",
            )
        scans: list[ScanResponse] = []
        for eid in entity_ids:
            resp = _start_one_scan(eid, requested_start, end, engine, executor, rate_limiter, connection_sem, http_client)
            if resp:
                scans.append(resp)
        return UniverseScanResponse(scans=scans, total_entities=len(entity_ids), universe=body.universe)

    resp = _start_one_scan(body.entity_id, requested_start, end, engine, executor, rate_limiter, connection_sem, http_client)
    if resp is None:
        raise HTTPException(status_code=422, detail="No windows to process — already up to date.")
    return resp


@router.get(
    "/scan/{scan_id}",
    dependencies=[Depends(require_api_key)],
    summary="Get scan status",
)
def get_scan_status(scan_id: str) -> dict:
    engine = get_engine()
    row = db_get_scan(engine, scan_id)
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Scan not found")
    return {
        "scan_id": row.scan_id,
        "entity_id": row.entity_id,
        "entity_name": row.entity_name,
        "status": row.status,
        "windows_total": row.windows_total,
        "windows_done": row.windows_done,
        "results": json.loads(row.results_json),
    }

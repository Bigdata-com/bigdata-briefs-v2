"""
Historical day-by-day scan for a single entity.

REST:
    POST /api/v1/scan              → start a scan, returns scan_id(s)
    GET  /api/v1/scan/status       → multi-entity aggregated scan status
    GET  /api/v1/scan/preview      → multi-entity resume preview (dry-run)
    GET  /api/v1/scan/{id}         → single scan status + results
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import date as date_cls, datetime, time as time_cls, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
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
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLUIScanRun,
)
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController

from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore
import httpx

from bigdata_briefs import logger

_ENTITY_COSTS_CSV = Path(__file__).parent.parent.parent / "data" / "universe_entity_costs.csv"
_UNIVERSES_DIR = Path(__file__).parent.parent.parent / "data" / "universes"


def _load_ticker_map_scan() -> dict[str, str]:
    """Return {entity_id: ticker} from all universe CSVs that have a ticker column."""
    mapping: dict[str, str] = {}
    for csv_path in _UNIVERSES_DIR.glob("*.csv"):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames and "ticker" in reader.fieldnames:
                    for row in reader:
                        if row.get("id") and row.get("ticker"):
                            mapping[row["id"]] = row["ticker"]
        except Exception:
            pass
    return mapping


_TICKER_MAP: dict[str, str] = _load_ticker_map_scan()

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
    boundary_time: time_cls | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return a list of (window_start, window_end) covering start→end day by day.

    When ``boundary_time`` is None (default) each window spans one UTC calendar day:
      - window_start: start of the day (or ``start`` for the first day)
      - window_end:   23:59:59 of the day, clipped by ``end``

    When ``boundary_time`` is set (e.g. time(13, 30) for 09:30 ET) each window runs
    from one occurrence of that time to the next, e.g. May 7 13:30 → May 8 13:30.
    """
    windows: list[tuple[datetime, datetime]] = []
    cursor = _ensure_utc_scan(start)
    end = _ensure_utc_scan(end)

    while cursor < end:
        if boundary_time is None:
            day_end = cursor.replace(hour=23, minute=59, second=59, microsecond=0)
            window_end = min(day_end, end)
            next_cursor = (cursor + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            next_boundary = cursor.replace(
                hour=boundary_time.hour, minute=boundary_time.minute,
                second=0, microsecond=0,
            )
            if next_boundary <= cursor:
                next_boundary += timedelta(days=1)
            # Skip Saturday (5) and Sunday (6): extend through the weekend to Monday
            while next_boundary.weekday() in (5, 6):
                next_boundary += timedelta(days=1)
            window_end = min(next_boundary, end)
            next_cursor = window_end
        windows.append((cursor, window_end))
        cursor = next_cursor

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


from bigdata_briefs.api.routes.universes import _UNIVERSES


class ScanRequest(BaseModel):
    entity_id: str | None = None   # required unless universe is set
    universe: str | None = None    # scan all entities in this universe
    start_date: str                # YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
    end_date: str | None = None    # YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS; defaults to now
    start_time: str | None = None  # HH:MM UTC — overrides the time on start_date
    end_time: str | None = None    # HH:MM UTC — overrides the time on end_date
    boundary_time: str | None = None  # HH:MM UTC — daily split point (default: 00:00 midnight)
    source_categories: list[str] | None = None  # override pipeline categories (news, news_premium, filings, transcripts)


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


def _parse_hhmm(value: str | None, field: str) -> time_cls | None:
    if not value:
        return None
    try:
        h, m = value.strip().split(":")
        return time_cls(int(h), int(m), 0)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail=f"{field} must be HH:MM (e.g. '13:30')")


def _parse_dates(start_date: str, end_date: str | None) -> tuple[datetime, datetime]:
    _FORMATS = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"]

    now = datetime.now(timezone.utc)
    today = now.date()

    def _parse(value: str, field: str) -> datetime:
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                if fmt == "%Y-%m-%d":
                    # date-only: start → 00:00:00; end → 23:59:59 unless it's today
                    if field == "start_date":
                        dt = dt.replace(hour=0, minute=0, second=0)
                    else:
                        dt = now if dt.date() == today else dt.replace(hour=23, minute=59, second=59)
                return dt
            except ValueError:
                continue
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS",
        )

    requested_start = _parse(start_date, "start_date")
    end = _parse(end_date, "end_date") if end_date else now
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
    source_categories: list[str] | None = None,
    boundary_time: time_cls | None = None,
) -> ScanResponse | None:
    """Create and submit one scan. Returns None if already up to date."""
    effective_start = resolve_scan_start(engine, entity_id, requested_start, end)
    windows = build_scan_windows(effective_start, end, boundary_time=boundary_time)
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
        source_categories=source_categories,
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
        "named universe). Runs the pipeline once per window from `start_date` to "
        "`end_date` (default: today). Resumes from the last completed window automatically.\n\n"
        "Single entity → returns a `ScanResponse` with one `scan_id`.\n"
        "Universe → returns a `UniverseScanResponse` with one `scan_id` per entity.\n\n"
        "**Window timing** — by default each window spans one UTC calendar day (midnight to midnight). "
        "Set `boundary_time` (`HH:MM` UTC) to shift the daily split point: `12:30` gives market-open "
        "to market-open windows (08:30 ET; `13:30` UTC in winter EST). "
        "Friday windows automatically extend through the weekend to Monday — five windows per week, no gaps. `start_time` (optional) sets the clock on `start_date` only; "
        "`end_time` (optional) sets the clock on `end_date` only. Set all three to the same value for "
        "a fully aligned range with no partial windows at the edges."
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

    start_t    = _parse_hhmm(body.start_time, "start_time")
    end_t      = _parse_hhmm(body.end_time, "end_time")
    boundary_t = _parse_hhmm(body.boundary_time, "boundary_time")

    if start_t:
        requested_start = requested_start.replace(
            hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
        )
    if end_t:
        end = end.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)

    if body.universe:
        entity_ids = _UNIVERSES.get(body.universe)
        if entity_ids is None:
            raise HTTPException(
                status_code=404,
                detail=f"Universe '{body.universe}' not found. Available: {list(_UNIVERSES)}",
            )
        scans: list[ScanResponse] = []
        for eid in entity_ids:
            resp = _start_one_scan(eid, requested_start, end, engine, executor, rate_limiter, connection_sem, http_client, source_categories=body.source_categories, boundary_time=boundary_t)
            if resp:
                scans.append(resp)
        return UniverseScanResponse(scans=scans, total_entities=len(entity_ids), universe=body.universe)

    resp = _start_one_scan(body.entity_id, requested_start, end, engine, executor, rate_limiter, connection_sem, http_client, source_categories=body.source_categories, boundary_time=boundary_t)
    if resp is None:
        raise HTTPException(status_code=422, detail="No windows to process — already up to date.")
    return resp


# ── GET /api/v1/scan/status ───────────────────────────────────────────────────


@router.get(
    "/scan/status",
    dependencies=[Depends(require_api_key)],
    summary="Multi-entity aggregated scan status",
    description=(
        "Poll scan progress for one or more entities over a date range. "
        "Returns per-entity per-day status rows plus aggregate completed/total counts.\n\n"
        "Query params:\n"
        "- `entity_ids`: comma-separated entity IDs\n"
        "- `start_date`: YYYY-MM-DD\n"
        "- `end_date`: YYYY-MM-DD"
    ),
)
def get_multi_scan_status(
    entity_ids: str = Query(..., description="Comma-separated entity IDs"),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
) -> dict:
    """Poll scan progress: returns per-entity per-day status."""
    engine = get_engine()
    ids = [x.strip() for x in entity_ids.split(",") if x.strip()]

    try:
        sd = date_cls.fromisoformat(start_date)
        ed = date_cls.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid dates — use YYYY-MM-DD")

    all_dates: list[str] = []
    d = sd
    while d <= ed:
        all_dates.append(d.isoformat())
        d += timedelta(days=1)

    requested_start_dt = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=timezone.utc)
    scan_range_end = datetime(ed.year, ed.month, ed.day, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as session:
        results: list[dict] = []
        for entity_id in ids:
            orch = session.get(SQLEntityOrchestrationState, entity_id)
            entity_name = (orch.kg_name if orch else None) or entity_id
            ticker = _TICKER_MAP.get(entity_id) or (orch.kg_ticker if orch else "") or ""

            eff = resolve_scan_start(engine, entity_id, requested_start_dt, scan_range_end)
            first_scan_day = eff.astimezone(timezone.utc).date()

            day_results: list[dict] = []
            for date_str in all_dates:
                td_obj = date_cls.fromisoformat(date_str)
                day_start = datetime(td_obj.year, td_obj.month, td_obj.day, 0, 0, 0, tzinfo=timezone.utc)
                day_end = datetime(td_obj.year, td_obj.month, td_obj.day, 23, 59, 59, 999999, tzinfo=timezone.utc)

                run = session.exec(
                    select(SQLEntityPipelineRunLog).where(
                        SQLEntityPipelineRunLog.entity_id == entity_id,
                        SQLEntityPipelineRunLog.report_window_end >= day_start,
                        SQLEntityPipelineRunLog.report_window_end <= day_end,
                    ).order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
                ).first()

                if run is None:
                    if first_scan_day is not None and td_obj < first_scan_day:
                        day_results.append({
                            "date": date_str,
                            "status": "skipped",
                            "reason": "before effective scan start (resume)",
                        })
                    else:
                        day_results.append({"date": date_str, "status": "pending"})
                    continue
                elif run.status == "running":
                    day_results.append({"date": date_str, "status": "running"})
                elif run.status in ("succeeded", "no_data"):
                    active = len(session.exec(
                        select(SQLBulletRunLog).where(
                            SQLBulletRunLog.run_id == run.run_id,
                            SQLBulletRunLog.is_active == True,  # noqa: E712
                        )
                    ).all())
                    discarded = len(session.exec(
                        select(SQLBulletRunLog).where(
                            SQLBulletRunLog.run_id == run.run_id,
                            SQLBulletRunLog.is_active == False,  # noqa: E712
                        )
                    ).all())
                    day_results.append({
                        "date": date_str,
                        "status": "succeeded",
                        "saved": active,
                        "discarded": discarded,
                    })
                else:
                    day_results.append({
                        "date": date_str,
                        "status": "failed",
                        "error": (run.error_summary or "")[:80],
                    })

            results.append({
                "entityId": entity_id,
                "entityName": entity_name,
                "ticker": ticker,
                "days": day_results,
            })

    total = len(ids) * len(all_dates)
    completed = sum(
        1 for r in results for dd in r["days"]
        if dd["status"] in ("succeeded", "failed", "skipped")
    )
    return {"entities": results, "total": total, "completed": completed}


# ── GET /api/v1/scan/preview ──────────────────────────────────────────────────


@router.get(
    "/scan/preview",
    dependencies=[Depends(require_api_key)],
    summary="Multi-entity resume preview (dry-run)",
    description=(
        "Returns per-entity resume point data without starting any scans.\n\n"
        "Query params:\n"
        "- `scope`: `entity` | `universe` | `all`\n"
        "- `entity_id`: required when scope=entity\n"
        "- `universe`: required when scope=universe"
    ),
)
def get_scan_preview(
    scope: str = Query("entity", description="entity | universe | all"),
    entity_id: str | None = Query(None, description="Required when scope=entity"),
    universe: str | None = Query(None, description="Required when scope=universe"),
) -> dict:
    """Return per-entity resume points for the update/scan preview."""
    from bigdata_briefs.api.routes.universes import _UNIVERSES

    engine = get_engine()
    now = datetime.now(timezone.utc)

    # Build CSV name lookup — used for name resolution and for scope=all entity list
    csv_names: dict[str, str] = {}
    if _ENTITY_COSTS_CSV.is_file():
        with _ENTITY_COSTS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                eid_c = row.get("entity_id", "").strip()
                name_c = row.get("name", "").strip()
                if eid_c and eid_c not in csv_names:
                    csv_names[eid_c] = name_c or eid_c

    # Resolve the set of entity_ids to preview
    if scope == "entity":
        if not entity_id:
            raise HTTPException(status_code=422, detail="entity_id required for scope=entity")
        eids = [entity_id]
    elif scope == "universe":
        if not universe:
            raise HTTPException(status_code=422, detail="universe required for scope=universe")
        eids = list(_UNIVERSES.get(universe) or [])
        if not eids:
            raise HTTPException(status_code=404, detail=f"universe '{universe}' not found or empty")
    elif scope == "all":
        # Use entities that exist in the DB (have been run at least once or are tracked).
        # CSV is only used as a name fallback, not as the entity source.
        with Session(engine) as session:
            eids = [r.entity_id for r in session.exec(select(SQLEntityOrchestrationState)).all()]
    else:
        raise HTTPException(status_code=422, detail=f"unknown scope '{scope}'")

    rows: list[dict] = []
    with Session(engine) as session:
        for eid in eids:
            orch = session.get(SQLEntityOrchestrationState, eid)
            name = (orch.kg_name if orch else None) or csv_names.get(eid) or eid
            ticker = (orch.kg_ticker if orch else None) or None

            last_run = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == eid)
                .where(SQLEntityPipelineRunLog.process_completed_at_utc.isnot(None))
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).first()

            if last_run is None or last_run.process_completed_at_utc is None:
                yesterday = (now - timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                fb_windows = build_scan_windows(yesterday, now)
                rows.append({
                    "entity_id": eid,
                    "name": name,
                    "ticker": ticker,
                    "last_run_at": None,
                    "resume_from": yesterday.isoformat(),
                    "resume_date": yesterday.date().isoformat(),
                    "est_windows": len(fb_windows),
                    "has_history": False,
                    "fallback": True,
                })
                continue

            last_at = (
                last_run.process_completed_at_utc.replace(tzinfo=timezone.utc)
                if last_run.process_completed_at_utc.tzinfo is None
                else last_run.process_completed_at_utc
            )
            req_start = last_at.replace(hour=0, minute=0, second=0, microsecond=0)
            effective_start = resolve_scan_start(engine, eid, req_start, now)
            windows = build_scan_windows(effective_start, now)

            rows.append({
                "entity_id": eid,
                "name": name,
                "ticker": ticker,
                "last_run_at": last_at.isoformat(),
                "resume_from": effective_start.isoformat(),
                "resume_date": effective_start.date().isoformat(),
                "est_windows": len(windows),
                "has_history": True,
            })

    runnable = [r for r in rows if r["est_windows"] > 0]
    total_windows = sum(r["est_windows"] for r in runnable)
    return {
        "scope": scope,
        "entities": rows,
        "runnable_count": len(runnable),
        "total_est_windows": total_windows,
    }


# ── GET /api/v1/scan/{scan_id} ────────────────────────────────────────────────


@router.get(
    "/scan/{scan_id}",
    dependencies=[Depends(require_api_key)],
    summary="Get scan status",
)
def get_single_scan_status(scan_id: str) -> dict:
    engine = get_engine()
    row = db_get_scan(engine, scan_id)
    if row is None:
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

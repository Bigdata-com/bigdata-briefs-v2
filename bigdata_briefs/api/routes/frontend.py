"""JSON endpoints feeding the React frontend (`/app`).

Three endpoints replace the static fixture files:
- /api/frontend/data.json     → window.DATA  (companies, composeEntities, composeSearchEntityIds, …)
- /api/frontend/extras.json   → window.EXTRAS (scan, history details, cost, activity)
- /api/frontend/run-data.json → window.RUN_DATA (recent runs, presets, log)

Where real data exists in the DB it is used; where no data exists yet
(live log stream, source presets) we fall back to a sensible default.
"""
from __future__ import annotations

import csv
import json
import uuid
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import threading
from fastapi import APIRouter
from pydantic import BaseModel as _BaseModel
from sqlalchemy import desc
from sqlmodel import Session, select

from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.routes.ui import (
    _DISCARD_STAGE_LABELS,
    _DISCARD_STAGE_ORDER,
    _load_bullets_for_run,
)
from bigdata_briefs.orchestration.earnings_calendar_cache import earnings_flags_for_calendar_day
from bigdata_briefs.orchestration.models import (
    SQLBatchParallelRun,
    SQLBulletRunLog,
    SQLEntityEarningsCalendar,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLRunMetrics,
    SQLRunNarrative,
    SQLUIBatchRun,
    SQLUIScanRun,
)
from bigdata_briefs.pricing import calculate_chunk_cost
from bigdata_briefs.settings import settings

router = APIRouter()

_UNIVERSES_DIR = Path(__file__).parent.parent.parent / "data" / "universes"


def _forensics_rejection_item_from_display_bullet(b: dict) -> dict:
    """One rejected row for JSON forensics — same ``discarded`` block as HTML (_render_discarded_detail_body)."""
    d = b.get("discarded") or {}
    return {
        "id": b["id"],
        "text": (b.get("text") or b.get("original_text") or "").strip() or "—",
        "score": d.get("score"),
        "discarded": d,
        "groundingFlag": b.get("grounding_decision"),
    }


def _build_forensics_rejection_groups(engine, run_id: uuid.UUID) -> tuple[int, int, list[dict]]:
    """(published_count, rejected_count, rejection_groups) aligned with ``_load_bullets_for_run`` / HTML."""
    display = _load_bullets_for_run(engine, run_id)
    published = sum(1 for b in display if b.get("is_active"))
    buckets: dict[str, list[dict]] = defaultdict(list)
    for b in display:
        if b.get("is_active"):
            continue
        stage = (b.get("discarded") or {}).get("stage") or "unknown"
        buckets[stage].append(_forensics_rejection_item_from_display_bullet(b))
    seen: set[str] = set()
    rejection_groups: list[dict] = []
    for stage_name in _DISCARD_STAGE_ORDER:
        items = buckets.get(stage_name)
        if not items:
            continue
        seen.add(stage_name)
        rejection_groups.append({
            "stage": stage_name,
            "stageLabel": _DISCARD_STAGE_LABELS.get(stage_name, stage_name.replace("_", " ").title()),
            "count": len(items),
            "items": items,
        })
    for stage_name, items in buckets.items():
        if stage_name not in seen and items:
            rejection_groups.append({
                "stage": stage_name,
                "stageLabel": _DISCARD_STAGE_LABELS.get(stage_name, stage_name.replace("_", " ").title()),
                "count": len(items),
                "items": items,
            })
    rejected = sum(1 for b in display if not b.get("is_active"))
    return published, rejected, rejection_groups

# Exchange codes from listing_values → display names, ordered by priority
_EXCHANGE_PRIORITY = ["XNYS", "XNAS", "XLON", "XPAR", "XETR", "XAMS", "XMIL", "XHKG", "XTKS"]
_EXCHANGE_NAMES = {
    "XNYS": "NYSE", "XNAS": "NASDAQ", "XLON": "LSE",
    "XPAR": "Euronext Paris", "XETR": "XETRA", "XAMS": "Euronext Amsterdam",
    "XMIL": "Borsa Italiana", "XHKG": "HKEX", "XTKS": "TSE",
}


_ENTITY_COSTS_CSV = Path(__file__).parent.parent.parent / "data" / "universe_entity_costs.csv"


def _load_compose_estimates() -> dict[str, dict]:
    """Load per-entity mean daily cost from universe_entity_costs.csv.

    Returns a dict {entity_id: {"costDisplay": "$X.XX"}} for all entities with a known cost.
    The CSV may list the same ``entity_id`` in more than one ``region`` (e.g. US + EU index
    overlap); we keep one display string per id by preferring curated ``universes`` rows over
    ``index_*_volume`` rows, then the higher numeric cost.
    """
    if not _ENTITY_COSTS_CSV.is_file():
        return {}

    def _compose_pick_key(row: dict[str, str], cost_val: float) -> tuple[int, float]:
        u = (row.get("universes") or "").strip()
        curated = 0 if u.startswith("index_") else 1
        return (curated, cost_val)

    best: dict[str, tuple[tuple[int, float], str]] = {}
    with _ENTITY_COSTS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eid = row.get("entity_id", "").strip()
            cost_str = row.get("mean_daily_cost_usd", "").strip()
            if not eid or not cost_str:
                continue
            try:
                cost_val = float(cost_str)
            except ValueError:
                continue
            if cost_val < 0.005:
                display = "< $0.01"
            else:
                display = f"${cost_val:.2f}"
            key = _compose_pick_key(row, cost_val)
            prev = best.get(eid)
            if prev is None or key > prev[0]:
                best[eid] = (key, display)
    return {eid: {"costDisplay": disp} for eid, (_, disp) in best.items()}


def _parse_kg_payload(kg_payload_json: str | None) -> dict:
    """Parse kg_payload_json and return clean entity metadata."""
    if not kg_payload_json:
        return {}
    try:
        payload = json.loads(kg_payload_json)
    except Exception:
        return {}

    listing_values: list[str] = payload.get("listing_values") or []
    ticker, exchange = "", ""
    for prefix in _EXCHANGE_PRIORITY:
        match = next((l for l in listing_values if l.startswith(prefix + ":")), None)
        if match:
            ticker = match.split(":")[1]
            exchange = _EXCHANGE_NAMES.get(prefix, prefix)
            break

    country_map = {"US": "United States", "GB": "United Kingdom", "FR": "France",
                   "DE": "Germany", "NL": "Netherlands", "JP": "Japan",
                   "CH": "Switzerland", "IT": "Italy", "ES": "Spain"}
    raw_country = payload.get("country", "")

    return {
        "ticker": ticker,
        "exchange": exchange,
        "sector": payload.get("sector") or "",
        "industry": payload.get("industry") or payload.get("industry_group") or "",
        "country": country_map.get(raw_country, raw_country),
        "countryCode": raw_country,
        "description": payload.get("description") or "",
        "webpage": payload.get("webpage") or "",
    }


def _load_ticker_map() -> dict[str, str]:
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


_TICKER_MAP: dict[str, str] = _load_ticker_map()


# Theme palette — kept empty; colors are now generated deterministically
# in the frontend via a string hash (see ThemeDot in shared.jsx).
_THEME_PALETTE: dict[str, str] = {}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _find_run_by_id_fragment(
    session: Session, fragment: str
) -> SQLEntityPipelineRunLog | None:
    """Match a run by full UUID string or by the first 8 hex chars shown in the UI."""
    frag = (fragment or "").strip()
    if not frag:
        return None
    # Full UUID: primary-key lookup (works for any age; old logic only scanned 200 rows).
    try:
        uid = uuid.UUID(frag)
        r = session.get(SQLEntityPipelineRunLog, uid)
        if r is not None and r.status in ("succeeded", "no_data"):
            return r
    except ValueError:
        pass
    raw = frag.lower().replace("-", "")
    if len(raw) >= 32:
        return None
    candidates = session.exec(
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
        .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        .limit(200)
    ).all()
    for r in candidates:
        rid = str(r.run_id).lower().replace("-", "")
        if rid.startswith(raw[:8]):
            return r
    return None


def _build_cost_dict_for_run(
    session: Session, latest_run: SQLEntityPipelineRunLog
) -> dict | None:
    """Full cost breakdown dict for one run (same shape as window.EXTRAS.cost)."""
    metrics = session.exec(
        select(SQLRunMetrics).where(SQLRunMetrics.run_id == latest_run.run_id)
    ).first()
    if not metrics:
        return None
    try:
        llm_models_raw = json.loads(metrics.llm_per_model_json or "[]")
        step_detail = json.loads(metrics.step_detail_json or "{}")
    except Exception:
        llm_models_raw = []
        step_detail = {}
    llm_models = [{
        "model": m.get("model", ""),
        "calls": m.get("n_calls", 0),
        "promptTokens": m.get("prompt_tokens", 0),
        "completionTokens": m.get("completion_tokens", 0),
        "totalTokens": m.get("total_tokens", 0),
        "cost": m.get("cost_usd", 0.0),
        "role": "",
    } for m in llm_models_raw]
    phases = []
    total_cost = (
        metrics.total_llm_cost_usd
        + metrics.total_embedding_cost_usd
        + calculate_chunk_cost(metrics.chunks_total)
    )
    for step_name, step in step_detail.items():
        ops = step.get("operational") or {}
        chunks_n = ops.get("chunks_retrieved", 0)
        api_cost = calculate_chunk_cost(chunks_n)
        phase_total = step.get("total_cost_usd", 0.0) + api_cost
        phases.append({
            "id": step_name,
            "label": step_name.replace("_", " ").title(),
            "llm": step.get("llm_cost_usd", 0.0),
            "embed": step.get("embedding_cost_usd", 0.0),
            "api": api_cost,
            "total": phase_total,
            "calls": step.get("llm_calls", 0),
            "chunks": chunks_n,
            "requests": ops.get("api_calls", 0),
            "percent": round(phase_total / total_cost * 100, 1) if total_cost else 0,
        })
    orch = session.get(SQLEntityOrchestrationState, latest_run.entity_id)
    return {
        "runId": str(latest_run.run_id)[:8],
        "entityName": (orch.kg_name if orch else None) or latest_run.entity_id,
        "entityId": latest_run.entity_id,
        "ticker": (orch.kg_ticker if orch else None) or "",
        "windowStart": _iso(latest_run.report_window_start),
        "windowEnd": _iso(latest_run.report_window_end),
        "durationSec": int(
            (latest_run.process_completed_at_utc - latest_run.process_started_at_utc).total_seconds()
        ) if latest_run.process_completed_at_utc else 0,
        "status": latest_run.status,
        "summary": {
            "llm": metrics.total_llm_cost_usd,
            "embeddings": metrics.total_embedding_cost_usd,
            "apiChunks": calculate_chunk_cost(metrics.chunks_total),
            "total": total_cost,
            "chunksTotal": metrics.chunks_total,
            "chunkRate": 0.0015,
            "embeddingTokens": metrics.embedding_tokens,
            "embeddingModel": metrics.embedding_model,
        },
        "llmModels": llm_models,
        "phases": phases,
        "apiPhases": [],
        "recentForBreakdown": [],
    }


def _build_recent_runs_for_cost(session: Session, limit: int = 25) -> list[dict]:
    """Sidebar list: recent runs that have metrics rows."""
    runs = session.exec(
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
        .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        .limit(80)
    ).all()
    out: list[dict] = []
    for r in runs:
        m = session.exec(
            select(SQLRunMetrics).where(SQLRunMetrics.run_id == r.run_id)
        ).first()
        if not m:
            continue
        orch = session.get(SQLEntityOrchestrationState, r.entity_id)
        total = (
            m.total_llm_cost_usd
            + m.total_embedding_cost_usd
            + calculate_chunk_cost(m.chunks_total)
        )
        dur_s = 0
        if r.process_completed_at_utc and r.process_started_at_utc:
            dur_s = int((r.process_completed_at_utc - r.process_started_at_utc).total_seconds())
        out.append({
            "runId": str(r.run_id)[:8],
            "entity": (orch.kg_name if orch else None) or r.entity_id,
            "ticker": _TICKER_MAP.get(r.entity_id) or (orch.kg_ticker if orch else "") or "",
            "cost": round(total, 4),
            "duration": _fmt_duration(float(dur_s)),
        })
        if len(out) >= limit:
            break
    return out


def _bullet_to_dict(b: SQLBulletRunLog) -> dict:
    novelty = "novel"
    if b.embedding_rewritten or (b.search_verdict == "rewrite"):
        novelty = "rewritten"
    citations = []
    try:
        for cit in json.loads(b.citations_json or "[]"):
            citations.append({
                "id": cit.get("id", ""),
                "source": cit.get("source_name", ""),
                "headline": cit.get("headline", ""),
                "date": cit.get("date", ""),
                "excerpt": cit.get("text", "") or "",
            })
    except Exception:
        pass
    return {
        "id": str(b.id),
        "trace_id": b.trace_id,
        "theme": b.theme or "Other",
        "novelty": novelty,
        "text": b.text,
        "rewrittenFrom": b.original_text if b.embedding_rewritten or b.search_verdict == "rewrite" else None,
        "rewriteReason": b.search_reason if b.search_verdict == "rewrite" else b.embedding_reason,
        "citations": citations,
        "relevance_score": b.relevance_score,
    }


def _discarded_to_dict(b: SQLBulletRunLog) -> dict:
    return {
        "id": str(b.id),
        "text": b.text or b.original_text,
        "stage": b.discard_stage or "unknown",
        "reason": (
            b.search_reason
            or b.embedding_reason
            or b.grounding_reason
            or b.relevance_reason
            or ""
        ),
        "score": b.relevance_score,
    }


def _build_companies(session: Session) -> list[dict]:
    rows = session.exec(select(SQLEntityOrchestrationState)).all()
    out = []
    for r in rows:
        kg = _parse_kg_payload(r.kg_payload_json)
        # Prefer ticker from universe CSV, then KG listing_values, then internal kg_ticker
        ticker = _TICKER_MAP.get(r.entity_id) or kg.get("ticker") or r.kg_ticker or ""
        out.append({
            "id": r.entity_id,
            "name": r.kg_name or r.entity_id,
            "ticker": ticker,
            "exchange": kg.get("exchange") or "",
            "sector": kg.get("sector") or "",
            "industry": kg.get("industry") or "",
            "country": kg.get("country") or "",
            "countryCode": kg.get("countryCode") or "",
            "description": kg.get("description") or "",
            "webpage": kg.get("webpage") or "",
        })
    return out


def _build_all_scan_entities(db_companies: list[dict]) -> list[dict]:
    """All entities available for scanning: DB companies (with full metadata) +
    CSV-only entities (name from CSV, empty ticker/exchange).  Sorted by rank in CSV."""
    db_by_id = {c["id"]: c for c in db_companies}
    out: list[dict] = []
    seen: set[str] = set()
    if _ENTITY_COSTS_CSV.is_file():
        with _ENTITY_COSTS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                eid = row.get("entity_id", "").strip()
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                if eid in db_by_id:
                    out.append(db_by_id[eid])
                else:
                    out.append({
                        "id": eid,
                        "name": row.get("name", "").strip() or eid,
                        "ticker": "",
                        "exchange": "", "sector": "", "industry": "",
                        "country": "", "countryCode": "",
                        "description": "", "webpage": "",
                    })
    # Append any DB companies not in the CSV (edge case)
    for c in db_companies:
        if c["id"] not in seen:
            out.append(c)
    return out


def _compose_entity_picks(companies: list[dict], *, max_picks: int = 9) -> list[dict]:
    """Compose (01 Entity): up to ``max_picks`` companies in ``top_us_100`` CSV order that exist in DB."""
    from bigdata_briefs.api.routes.universes import _UNIVERSES

    order = list(_UNIVERSES.get("top_us_100") or [])
    by_id = {c["id"]: c for c in companies}
    out: list[dict] = []
    for eid in order:
        row = by_id.get(eid)
        if row is not None:
            out.append(row)
            if len(out) >= max_picks:
                break
    return out


def _all_universe_entity_ids() -> list[str]:
    """Stable union of entity IDs across every CSV-backed universe (Compose search allow-list)."""
    from bigdata_briefs.api.routes.universes import _UNIVERSES

    seen: set[str] = set()
    ordered: list[str] = []
    for ids in _UNIVERSES.values():
        for eid in ids:
            if eid and eid not in seen:
                seen.add(eid)
                ordered.append(eid)
    return ordered


def _build_company_summaries(session: Session) -> dict[str, dict]:
    """Return per-company summary for the sidebar: latest bullet count + 7-day pulse bars."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    orches = session.exec(select(SQLEntityOrchestrationState)).all()
    result: dict[str, dict] = {}

    for orch in orches:
        entity_id = orch.entity_id

        # Latest succeeded run → bullet count
        latest = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            )
            .order_by(desc(SQLEntityPipelineRunLog.report_window_end))
        ).first()

        bullets_saved = 0
        bullets_discarded = 0
        if latest:
            # Aggregate across all runs that ended on the same calendar day as latest,
            # mirroring _build_brief_for_day so the list preview matches the brief.
            latest_day = latest.report_window_end.date().isoformat() if latest.report_window_end else None
            day_run_ids = [
                r.run_id for r in session.exec(
                    select(SQLEntityPipelineRunLog).where(
                        SQLEntityPipelineRunLog.entity_id == entity_id,
                        SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                    )
                ).all()
                if r.report_window_end and r.report_window_end.date().isoformat() == latest_day
            ]
            for rid in day_run_ids:
                day_bullets = session.exec(
                    select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == rid)
                ).all()
                bullets_saved     += sum(1 for b in day_bullets if b.is_active)
                bullets_discarded += sum(1 for b in day_bullets if not b.is_active)

        # Last 7 days pulse (aggregated by window date)
        recent_runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                SQLEntityPipelineRunLog.report_window_end >= cutoff,
            )
            .order_by(SQLEntityPipelineRunLog.report_window_end)
        ).all()

        by_day: dict[str, int] = {}
        for r in recent_runs:
            if r.report_window_end is None:
                continue
            day = r.report_window_end.date().isoformat()
            bullets = session.exec(
                select(SQLBulletRunLog).where(
                    SQLBulletRunLog.run_id == r.run_id,
                    SQLBulletRunLog.is_active == True,  # noqa: E712
                )
            ).all()
            by_day[day] = by_day.get(day, 0) + len(bullets)

        pulse7 = [{"date": d, "saved": v} for d, v in sorted(by_day.items())]
        last_run_date = (
            latest.report_window_end.strftime("%Y-%m-%dT%H:%MZ")
            if latest and latest.report_window_end
            else None
        )

        total_runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            )
        ).all()

        result[entity_id] = {
            "bulletsSaved": bullets_saved,
            "bulletsDiscarded": bullets_discarded,
            "lastRunDate": last_run_date,
            "pulse7": pulse7,
            "totalRuns": len(total_runs),
        }

    return result


def _global_brief_calendar_days(session: Session) -> list[str]:
    """Sorted YYYY-MM-DD (UTC) with at least one succeeded/no_data run for *some* entity.

    Brief date rail: step only across days where the desk actually published a window for
    any company — not contiguous gaps, and not limited to the ticker currently on screen.
    """
    ends = session.exec(
        select(SQLEntityPipelineRunLog.report_window_end)
        .where(
            SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            SQLEntityPipelineRunLog.report_window_end.isnot(None),  # type: ignore[union-attr]
        )
    ).all()
    days = {dt.date().isoformat() for dt in ends if dt is not None}
    return sorted(days)


def _empty_brief_for_calendar_day(session: Session, entity_id: str, day_iso: str) -> dict:
    """Placeholder brief when there is no succeeded/no_data run ending on ``day_iso`` (YYYY-MM-DD)."""
    d = date_cls.fromisoformat(day_iso)
    ws = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    we = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
    orch = session.get(SQLEntityOrchestrationState, entity_id)
    kg = _parse_kg_payload(orch.kg_payload_json if orch else None)
    ticker = _TICKER_MAP.get(entity_id) or kg.get("ticker") or (orch.kg_ticker if orch else "") or ""
    return {
        "entityId": entity_id,
        "entityName": (orch.kg_name if orch else None) or entity_id,
        "ticker": ticker,
        "exchange": kg.get("exchange") or "",
        "sector": kg.get("sector") or "",
        "industry": kg.get("industry") or kg.get("industry_group") or "",
        "country": kg.get("country") or "",
        "description": kg.get("description") or "",
        "webpage": kg.get("webpage") or "",
        "runId": "—",
        "windowStart": _iso(ws),
        "windowEnd": _iso(we),
        "runCreatedAt": None,
        "durationSec": 0,
        "bulletsSaved": 0,
        "bulletsDiscarded": 0,
        "chunksReviewed": 0,
        "sourcesScanned": 0,
        "narrative": None,
        "themes": [],
        "bullets": [],
        "discarded": [],
        "noRunForWindow": True,
    }


def _latest_succeeded_run(session: Session, entity_id: str) -> SQLEntityPipelineRunLog | None:
    """Newest reporting window for this entity (not newest *job finish time*).

    Ordering by ``process_completed_at_utc`` made a backfilled older window appear as the
    default brief after a newer calendar run — and broke mental alignment with
    ``availableDates``, which is keyed by ``report_window_end``."""
    return session.exec(
        select(SQLEntityPipelineRunLog)
        .where(
            SQLEntityPipelineRunLog.entity_id == entity_id,
            SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            SQLEntityPipelineRunLog.report_window_end.isnot(None),  # type: ignore[union-attr]
        )
        .order_by(
            desc(SQLEntityPipelineRunLog.report_window_end),
            desc(SQLEntityPipelineRunLog.process_completed_at_utc),
        )
    ).first()


def _build_brief(session: Session, run: SQLEntityPipelineRunLog) -> dict:
    """Build a brief from a single run. Used for todays_brief on the home page."""
    return _build_brief_for_day(session, [run])


def _build_brief_for_day(session: Session, runs: list[SQLEntityPipelineRunLog]) -> dict:
    """Build a combined brief from all runs on the same calendar day.

    Bullets from every run are merged; stats are summed; the coverage window
    spans from the earliest run's window_start to the latest run's window_end.
    """
    if not runs:
        return {}

    # Sort chronologically so bullets appear in temporal order and coverage dates are correct
    runs_sorted = sorted(
        runs,
        key=lambda r: r.report_window_start or datetime.min.replace(tzinfo=timezone.utc),
    )
    latest_run = runs_sorted[-1]

    active_all: list[dict] = []
    discarded_all: list[dict] = []
    active_count_by_run: dict = {}
    total_chunks = 0
    total_sources = 0
    total_duration = 0.0

    for run in runs_sorted:
        bullets = session.exec(
            select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run.run_id)
        ).all()
        run_active = [b for b in bullets if b.is_active]
        active_all.extend([_bullet_to_dict(b) for b in run_active])
        discarded_all.extend([_discarded_to_dict(b) for b in bullets if not b.is_active])
        active_count_by_run[run.run_id] = len(run_active)

        metrics = session.exec(
            select(SQLRunMetrics).where(SQLRunMetrics.run_id == run.run_id)
        ).first()
        if metrics:
            total_chunks   += metrics.chunks_total or 0
            total_sources  += metrics.sources_scanned or 0

        if run.process_completed_at_utc and run.process_started_at_utc:
            total_duration += (run.process_completed_at_utc - run.process_started_at_utc).total_seconds()

    # Theme grouping across all runs
    theme_counts: dict[str, int] = defaultdict(int)
    for b in active_all:
        theme_counts[b["theme"]] += 1
    themes = [{"name": t, "count": c} for t, c in theme_counts.items()]

    # Narrative: last run (chronologically) that has at least one active bullet
    narrative_run = next(
        (r for r in reversed(runs_sorted) if active_count_by_run.get(r.run_id, 0) > 0),
        latest_run,
    )
    narrative = session.exec(
        select(SQLRunNarrative)
        .where(SQLRunNarrative.run_id == narrative_run.run_id)
        .order_by(desc(SQLRunNarrative.created_at))
    ).first()

    orch = session.get(SQLEntityOrchestrationState, latest_run.entity_id)
    kg = _parse_kg_payload(orch.kg_payload_json if orch else None)
    ticker = _TICKER_MAP.get(latest_run.entity_id) or kg.get("ticker") or (orch.kg_ticker if orch else "") or ""

    # Coverage = first run start → last run end
    coverage_start = runs_sorted[0].report_window_start
    coverage_end   = latest_run.report_window_end

    return {
        "entityId": latest_run.entity_id,
        "entityName": (orch.kg_name if orch else None) or latest_run.entity_id,
        "ticker": ticker,
        "exchange": kg.get("exchange") or "",
        "sector": kg.get("sector") or "",
        "industry": kg.get("industry") or "",
        "country": kg.get("country") or "",
        "description": kg.get("description") or "",
        "webpage": kg.get("webpage") or "",
        "runId": str(latest_run.run_id)[:8],
        "windowStart":    _iso(coverage_start),
        "windowEnd":      _iso(coverage_end),
        "coverageStart":  _iso(coverage_start),
        "coverageEnd":    _iso(coverage_end),
        "runCreatedAt": _iso(latest_run.process_completed_at_utc),
        "durationSec": int(total_duration),
        "bulletsSaved": len(active_all),
        "bulletsDiscarded": len(discarded_all),
        "chunksReviewed": total_chunks,
        "sourcesScanned": total_sources,
        "narrative": narrative.narrative_text if narrative else None,
        "themes": themes,
        "bullets": active_all,
        "discarded": discarded_all,
    }


def _build_pulse(session: Session, entity_id: str, end_date: str | None = None) -> list[dict]:
    """Return 14-day pulse ending on end_date (YYYY-MM-DD). Defaults to today."""
    if end_date:
        from datetime import date as _date
        td = _date.fromisoformat(end_date)
        cutoff = datetime(td.year, td.month, td.day, 23, 59, 59) - timedelta(days=13)
        ceiling = datetime(td.year, td.month, td.day, 23, 59, 59)
    else:
        ceiling = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = ceiling - timedelta(days=13)

    rows = session.exec(
        select(SQLEntityPipelineRunLog)
        .where(
            SQLEntityPipelineRunLog.entity_id == entity_id,
            SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            SQLEntityPipelineRunLog.report_window_end >= cutoff,
            SQLEntityPipelineRunLog.report_window_end <= ceiling,
        )
    ).all()
    by_day: dict[str, dict] = {}
    for r in rows:
        if r.report_window_end is None:
            continue
        day = r.report_window_end.date().isoformat()
        bullets = session.exec(
            select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == r.run_id)
        ).all()
        saved = sum(1 for b in bullets if b.is_active)
        discarded = sum(1 for b in bullets if not b.is_active)
        if day in by_day:
            by_day[day]["saved"] += saved
            by_day[day]["discarded"] += discarded
        else:
            by_day[day] = {"date": day, "saved": saved, "discarded": discarded}
    return sorted(by_day.values(), key=lambda x: x["date"])


def _build_history(session: Session, entity_id: str, limit: int = 30) -> list[dict]:
    rows = session.exec(
        select(SQLEntityPipelineRunLog)
        .where(
            SQLEntityPipelineRunLog.entity_id == entity_id,
            SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
        )
        .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        .limit(limit)
    ).all()
    out = []
    for r in rows:
        bullets = session.exec(
            select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == r.run_id)
        ).all()
        active = [b for b in bullets if b.is_active]
        discarded = [b for b in bullets if not b.is_active]
        themes = sorted({b.theme for b in active if b.theme})
        narrative = session.exec(
            select(SQLRunNarrative).where(SQLRunNarrative.run_id == r.run_id)
        ).first()
        date = r.report_window_end.date().isoformat() if r.report_window_end else ""
        out.append({
            "runId": str(r.run_id)[:8],
            "date": date,
            "saved": len(active),
            "discarded": len(discarded),
            "themes": themes,
            "narrative": narrative.narrative_text if narrative else "",
            "bullets": [{"text": b.text, "theme": b.theme or ""} for b in active],
        })
    return out


def _build_batches(session: Session, limit: int = 20) -> list[dict]:
    out = []
    ui_batches = session.exec(
        select(SQLUIBatchRun).order_by(desc(SQLUIBatchRun.created_at)).limit(limit)
    ).all()
    for b in ui_batches:
        try:
            results = json.loads(b.results_json or "[]")
        except Exception:
            results = []
        succeeded = sum(1 for r in results if r.get("status") == "succeeded")
        failed = sum(1 for r in results if r.get("status") == "failed")
        running = b.total - b.done if b.status == "running" else 0
        dur = None
        if b.updated_at and b.created_at:
            dur = (b.updated_at - b.created_at).total_seconds()
        out.append({
            "id": b.batch_id,
            "started": _iso(b.created_at),
            "status": b.status,
            "total": b.total,
            "succeeded": succeeded,
            "failed": failed,
            "running": running,
            "type": "ui-parallel",
            "duration": _fmt_duration(dur),
            "universe": "",
        })
    api_batches = session.exec(
        select(SQLBatchParallelRun).order_by(desc(SQLBatchParallelRun.submitted_at)).limit(limit)
    ).all()
    for b in api_batches:
        out.append({
            "id": str(b.batch_id),
            "started": _iso(b.submitted_at),
            "status": "submitted",
            "total": b.total,
            "succeeded": b.total,
            "failed": 0,
            "running": 0,
            "type": "api-parallel",
            "duration": "—",
            "universe": "",
        })
    out.sort(key=lambda x: x.get("started") or "", reverse=True)
    return out[:limit]


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("/data.json")
def get_data() -> dict:
    """Maps to window.DATA in the React frontend."""
    engine = get_engine()
    with Session(engine) as session:
        companies = _build_companies(session)
        all_scan_entities = _build_all_scan_entities(companies)
        compose_entities = _compose_entity_picks(companies)
        compose_search_ids = _all_universe_entity_ids()
        company_summaries = _build_company_summaries(session)

        # Today's brief: most recent succeeded run across ALL entities.
        latest_run = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).first()

        if latest_run:
            latest_day = latest_run.report_window_end.date().isoformat() if latest_run.report_window_end else None
            home_day_runs = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(
                    SQLEntityPipelineRunLog.entity_id == latest_run.entity_id,
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                )
            ).all()
            home_day_runs = [r for r in home_day_runs if r.report_window_end and r.report_window_end.date().isoformat() == latest_day]
            todays_brief = _build_brief_for_day(session, home_day_runs if home_day_runs else [latest_run])
        else:
            todays_brief = None
        pulse = _build_pulse(session, latest_run.entity_id) if latest_run else []
        history = _build_history(session, latest_run.entity_id) if latest_run else []
        batches = _build_batches(session)

        # Brief date rail: any calendar day where at least one company has a run
        available_dates = _global_brief_calendar_days(session)

    return {
        "companies": companies,
        "allScanEntities": all_scan_entities,
        "composeEntities": compose_entities,
        "composeSearchEntityIds": compose_search_ids,
        "companySummaries": company_summaries,
        "todaysBrief": todays_brief,
        "availableDates": available_dates,
        "pulse": pulse,
        "history": history,
        "batches": batches,
        "rateStatus": {
            "queriesInWindow": 0,
            "windowCapacity": 460,
            "windowSeconds": 60,
            "connSemAvailable": 20,
            "connSemCapacity": 20,
            "maxConcurrent": 10,
            "inFlight": 0,
            "queueDepth": 0,
        },
        "themePalette": _THEME_PALETTE,
        "noveltyDays": settings.NOVELTY_LOOKBACK_DAYS,
    }


def _extras_universe_cards() -> list[dict[str, str | int]]:
    """Scan-page universe picker: every CSV-backed universe (same registry as REST ``/universes``)."""
    from bigdata_briefs.api.routes.universes import _UNIVERSES

    labels: dict[str, tuple[str, str]] = {
        "dow_30": ("DOW 30", "Dow Jones Industrial Average"),
        "eurostoxx_50": ("EuroStoxx 50", "Eurozone blue-chip index"),
        "top_us_10": ("Top US 10", "Ten largest US listings"),
        "top_us_100": ("Top US 100", "Top US listings by market cap"),
        "top_us_500": ("Top US 500", "Broad US large-cap universe"),
        "top_eu_100": ("Top EU 100", "European large caps"),
        "top_eu_500": ("Top EU 500", "European wide large-cap universe"),
    }
    cards: list[dict[str, str | int]] = []
    for uid in sorted(_UNIVERSES.keys()):
        ids = _UNIVERSES.get(uid) or []
        n = len(ids)
        if uid in labels:
            label, desc = labels[uid]
        else:
            label = uid.replace("_", " ").title().replace("Us ", "US ").replace("Eu ", "EU ")
            desc = f"{n} entities"
        cards.append({"id": uid, "label": label, "count": n, "description": desc, "entity_ids": ids})
    return cards


@router.get("/extras.json")
def get_extras() -> dict:
    """Maps to window.EXTRAS in the React frontend."""
    engine = get_engine()
    with Session(engine) as session:
        # Past scans
        scans = session.exec(
            select(SQLUIScanRun).order_by(desc(SQLUIScanRun.created_at)).limit(20)
        ).all()
        past_scans = []
        for s in scans:
            try:
                results = json.loads(s.results_json or "[]")
            except Exception:
                results = []
            failed = sum(1 for r in results if r.get("status") == "failed")
            dur = None
            if s.updated_at and s.created_at:
                dur = (s.updated_at - s.created_at).total_seconds()
            past_scans.append({
                "id": s.scan_id,
                "entity": s.entity_name,
                "ticker": "",
                "range": "",
                "windows": s.windows_total,
                "completed": s.windows_done,
                "failed": failed,
                "status": s.status,
                "duration": _fmt_duration(dur),
                "started": _iso(s.created_at),
            })

        # Active scan (most recent running, else most recent)
        active_scan = scans[0] if scans else None
        scan_dict = None
        if active_scan:
            try:
                results = json.loads(active_scan.results_json or "[]")
            except Exception:
                results = []
            days = []
            for r in results:
                day = {
                    "date": r.get("window_start", "")[:10],
                    "status": r.get("status", "pending"),
                    "saved": r.get("saved", 0),
                    "discarded": r.get("discarded", 0),
                }
                if r.get("error"):
                    day["error"] = r["error"]
                days.append(day)
            elapsed = 0
            if active_scan.updated_at and active_scan.created_at:
                elapsed = int((active_scan.updated_at - active_scan.created_at).total_seconds())
            scan_dict = {
                "entityName": active_scan.entity_name,
                "entityId": active_scan.entity_id,
                "universe": "",
                "startDate": "",
                "endDate": "",
                "totalDays": active_scan.windows_total,
                "completedDays": active_scan.windows_done,
                "failedDays": sum(1 for d in days if d.get("status") == "failed"),
                "skippedDays": 0,
                "elapsedSec": elapsed,
                "estRemainingSec": 0,
                "sources": [],
                "days": days,
                "pastScans": past_scans,
            }
        else:
            scan_dict = {
                "entityName": "",
                "entityId": "",
                "universe": "",
                "startDate": "",
                "endDate": "",
                "totalDays": 0,
                "completedDays": 0,
                "failedDays": 0,
                "skippedDays": 0,
                "elapsedSec": 0,
                "estRemainingSec": 0,
                "sources": [],
                "days": [],
                "pastScans": past_scans,
            }

        # History details: same shape as today's brief but with rejection groups
        latest_run = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).first()
        history_days = []
        if latest_run:
            recent_runs = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(
                    SQLEntityPipelineRunLog.entity_id == latest_run.entity_id,
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                )
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
                .limit(5)
            ).all()
            for r in recent_runs:
                published, rejected_n, rejection_groups = _build_forensics_rejection_groups(engine, r.run_id)
                date = r.report_window_end.date().isoformat() if r.report_window_end else ""
                history_days.append({
                    "date": date,
                    "runId": str(r.run_id)[:8],
                    "published": published,
                    "rejected": rejected_n,
                    "rejectionGroups": rejection_groups,
                })

        # Cost breakdown for the latest run
        cost_dict = None
        if latest_run:
            cost_dict = _build_cost_dict_for_run(session, latest_run)
            if cost_dict is not None:
                cost_dict["recentForBreakdown"] = _build_recent_runs_for_cost(session)

        # Activity log
        ui_batches = session.exec(
            select(SQLUIBatchRun).order_by(desc(SQLUIBatchRun.created_at)).limit(10)
        ).all()
        api_batches = session.exec(
            select(SQLBatchParallelRun).order_by(desc(SQLBatchParallelRun.submitted_at)).limit(10)
        ).all()
        ui_activity = []
        for b in ui_batches:
            ui_activity.append({
                "id": b.batch_id,
                "started": _iso(b.created_at),
                "status": b.status,
                "total": b.total,
                "done": b.done,
                "failed": 0,
                "duration": _fmt_duration((b.updated_at - b.created_at).total_seconds()) if b.updated_at and b.created_at else "—",
                "saved": 0,
                "discarded": 0,
                "cost": 0.0,
                "source": "ui-parallel",
            })
        api_activity = []
        for b in api_batches:
            api_activity.append({
                "id": str(b.batch_id),
                "started": _iso(b.submitted_at),
                "status": "submitted",
                "total": b.total,
                "done": b.total,
                "failed": 0,
                "duration": "—",
                "saved": 0,
                "discarded": 0,
                "cost": 0.0,
                "source": "api",
            })

    return {
        "scan": scan_dict,
        "historyDetails": {"days": history_days},
        "cost": cost_dict,
        "activity": {"uiBatches": ui_activity, "apiBatches": api_activity},
        "universes": _extras_universe_cards(),
    }


@router.get("/companies/summaries")
def get_companies_summaries(date: str | None = None) -> dict:
    """Return per-company sidebar summaries for a given window date (YYYY-MM-DD).

    bulletsSaved = active bullets on that exact date.
    hasRunOnDate = whether a succeeded/no_data run exists for that calendar day (including empty briefs).
    pulse7 = 7-day window ending on that date (last 7 days including target).
    When date is omitted uses the most recent available date in the DB.
    """
    engine = get_engine()
    with Session(engine) as session:
        # Resolve the target date
        if date:
            target_date = date
        else:
            latest = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
                .order_by(desc(SQLEntityPipelineRunLog.report_window_end))
            ).first()
            target_date = latest.report_window_end.date().isoformat() if latest and latest.report_window_end else None

        if not target_date:
            return {"summaries": {}, "date": None}

        from datetime import date as date_type
        td = date_type.fromisoformat(target_date)
        window_start = td - timedelta(days=6)  # 7-day window ending on target_date

        orches = session.exec(select(SQLEntityOrchestrationState)).all()
        summaries: dict[str, dict] = {}

        for orch in orches:
            entity_id = orch.entity_id

            # Bullets saved on the exact target date — aggregate across all runs that day
            runs_on_date = session.exec(
                select(SQLEntityPipelineRunLog).where(
                    SQLEntityPipelineRunLog.entity_id == entity_id,
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                    SQLEntityPipelineRunLog.report_window_end >= datetime(td.year, td.month, td.day, 0, 0, 0),
                    SQLEntityPipelineRunLog.report_window_end <= datetime(td.year, td.month, td.day, 23, 59, 59),
                )
            ).all()

            bullets_saved = 0
            bullets_discarded = 0
            for run_on_date in runs_on_date:
                all_bullets = session.exec(
                    select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_on_date.run_id)
                ).all()
                bullets_saved     += sum(1 for b in all_bullets if b.is_active)
                bullets_discarded += sum(1 for b in all_bullets if not b.is_active)

            has_run_on_date = len(runs_on_date) > 0

            # 7-day pulse ending on target_date
            recent_runs = session.exec(
                select(SQLEntityPipelineRunLog).where(
                    SQLEntityPipelineRunLog.entity_id == entity_id,
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                    SQLEntityPipelineRunLog.report_window_end >= datetime(window_start.year, window_start.month, window_start.day),
                    SQLEntityPipelineRunLog.report_window_end <= datetime(td.year, td.month, td.day, 23, 59, 59),
                ).order_by(SQLEntityPipelineRunLog.report_window_end)
            ).all()

            by_day: dict[str, int] = {}
            for r in recent_runs:
                if r.report_window_end is None:
                    continue
                day = r.report_window_end.date().isoformat()
                active_b = session.exec(
                    select(SQLBulletRunLog).where(
                        SQLBulletRunLog.run_id == r.run_id,
                        SQLBulletRunLog.is_active == True,  # noqa: E712
                    )
                ).all()
                by_day[day] = by_day.get(day, 0) + len(active_b)

            pulse7 = [{"date": d, "saved": v} for d, v in sorted(by_day.items())]

            latest_ever = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(
                    SQLEntityPipelineRunLog.entity_id == entity_id,
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                )
                .order_by(desc(SQLEntityPipelineRunLog.report_window_end))
            ).first()
            last_run_date = (
                latest_ever.report_window_end.strftime("%Y-%m-%dT%H:%MZ")
                if latest_ever and latest_ever.report_window_end
                else None
            )

            summaries[entity_id] = {
                "bulletsSaved": bullets_saved,
                "bulletsDiscarded": bullets_discarded,
                "hasRunOnDate": has_run_on_date,
                "lastRunDate": last_run_date,
                "pulse7": pulse7,
            }

        # Earnings on selected day: read per-entity cache written at quarter_info (no live API).
        eids = [orch.entity_id for orch in orches]
        cache_rows: list[SQLEntityEarningsCalendar] = []
        if eids:
            cache_rows = list(
                session.exec(select(SQLEntityEarningsCalendar).where(SQLEntityEarningsCalendar.entity_id.in_(eids))).all()
            )
        cache_by = {r.entity_id: r for r in cache_rows}
        for entity_id, summ in summaries.items():
            hit = cache_by.get(entity_id)
            if hit and hit.earnings_events_json:
                on_date, sess_title = earnings_flags_for_calendar_day(hit.earnings_events_json, td)
                summ["earningsOnDate"] = on_date
                summ["earningsSessionTitle"] = sess_title
            else:
                summ["earningsOnDate"] = False
                summ["earningsSessionTitle"] = None

    return {"summaries": summaries, "date": target_date}


@router.get("/entity/{entity_id}/brief")
def get_entity_brief(entity_id: str, date: str | None = None) -> dict:
    """Return the brief for a single entity, optionally for a specific window date (YYYY-MM-DD).

    When ``date`` is omitted returns the most recent run (by reporting window).
    ``availableDates`` lists every UTC calendar day on which *any* entity has a succeeded/no_data
    run (desk-wide publication days). For the current entity, that day may have no run — then a
    placeholder brief is returned so prev/next can still move along the shared calendar.
    """
    engine = get_engine()
    with Session(engine) as session:
        all_runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            )
            .order_by(SQLEntityPipelineRunLog.report_window_end)
        ).all()
        available_dates = _global_brief_calendar_days(session)

        run: SQLEntityPipelineRunLog | None = None
        if date:
            day = date.strip()[:10]
            run = next(
                (r for r in reversed(all_runs)
                 if r.report_window_end and r.report_window_end.date().isoformat() == day),
                None,
            )
            if run is None:
                if day in available_dates:
                    pulse = _build_pulse(session, entity_id, end_date=day)
                    history = _build_history(session, entity_id)
                    return {
                        "brief": _empty_brief_for_calendar_day(session, entity_id, day),
                        "pulse": pulse,
                        "history": history,
                        "availableDates": available_dates,
                        "selectedDate": day,
                    }
                if all_runs:
                    run = _latest_succeeded_run(session, entity_id)
        else:
            run = _latest_succeeded_run(session, entity_id)

        if not run:
            return {
                "error": "no runs found",
                "entityId": entity_id,
                "availableDates": available_dates,
            }

        end = run.report_window_end.date().isoformat() if run.report_window_end else None

        # Aggregate ALL runs that ended on the same calendar day
        day_runs = [r for r in all_runs if r.report_window_end and r.report_window_end.date().isoformat() == end]
        brief = _build_brief_for_day(session, day_runs if day_runs else [run])

        pulse = _build_pulse(session, entity_id, end_date=end)
        history = _build_history(session, entity_id)

    return {
        "brief": brief,
        "pulse": pulse,
        "history": history,
        "availableDates": available_dates,
        "selectedDate": end,
    }


class FrontendRunRequest(_BaseModel):
    entity_id: str
    window: str = "24h"          # "24h" = incremental (today UTC / since last run); "custom" = explicit dates
    custom_start: str | None = None  # YYYY-MM-DD
    custom_end: str | None = None
    source_categories: list[str] = ["news"]


def _run_worker(
    entity_id: str,
    pipeline_config: dict,
    force_start: datetime | None,
    force_end: datetime | None,
    run_id_override: uuid.UUID,
    engine_str: str,
) -> None:
    """Background thread: run the pipeline then generate narrative."""
    from sqlalchemy import create_engine as _ce
    from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
    from bigdata_briefs.orchestration.db import ensure_orchestration_schema
    from pathlib import Path

    eng = _ce(engine_str, echo=False)
    ensure_orchestration_schema(eng)

    run_entity_incremental(
        entity_id=entity_id,
        pipeline_config=pipeline_config,
        state_dir=Path(".brief_pipeline_state"),
        force_window_start=force_start,
        force_window_end=force_end,
        force_run=True,
        engine=eng,
        run_id=run_id_override,
    )


@router.post("/run")
def frontend_start_run(body: FrontendRunRequest) -> dict:
    """Launch a single-entity pipeline run from the React Compose page.

    Returns immediately with ``run_id`` — the caller should poll
    ``GET /api/frontend/run/{run_id}`` for status.
    """
    engine = get_engine()

    # Resolve window dates
    now = datetime.now(timezone.utc)
    force_start: datetime | None = None
    force_end: datetime | None = now

    if body.window == "custom" and body.custom_start and body.custom_end:
        force_start = datetime.fromisoformat(body.custom_start).replace(
            hour=0, minute=0, second=0, tzinfo=timezone.utc
        )
        force_end = datetime.fromisoformat(body.custom_end).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    elif body.window == "24h":
        force_start = None  # incremental default
        force_end = None
    # else "custom" without dates → incremental

    # Build pipeline config — categories from source selection
    from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
    pipeline_config = load_pipeline_config_dict(resolve_config_path(None))
    if body.source_categories:
        pipeline_config["categories"] = body.source_categories

    run_id = uuid.uuid4()

    thread = threading.Thread(
        target=_run_worker,
        args=(
            body.entity_id,
            pipeline_config,
            force_start,
            force_end,
            run_id,
            settings.DB_STRING,
        ),
        daemon=True,
    )
    thread.start()

    # Resolve entity name for the response
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, body.entity_id)
    entity_name = (orch.kg_name if orch else None) or body.entity_id

    return {
        "run_id": str(run_id),
        "entity_id": body.entity_id,
        "entity_name": entity_name,
        "status": "queued",
    }


@router.get("/run/{run_id}")
def frontend_run_status(run_id: str) -> dict:
    """Poll the status of a single pipeline run launched via POST /api/frontend/run."""
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return {"error": "invalid run_id"}

    engine = get_engine()
    with Session(engine) as session:
        run = session.get(SQLEntityPipelineRunLog, rid)
        if not run:
            return {"run_id": run_id, "status": "queued"}

        orch = session.get(SQLEntityOrchestrationState, run.entity_id)
        narrative = session.exec(
            select(SQLRunNarrative).where(SQLRunNarrative.run_id == rid)
        ).first()

        bullets_active: list[dict] = []
        bullets_discarded: list[dict] = []
        if run.status in ("succeeded", "no_data"):
            all_bullets = session.exec(
                select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == rid)
            ).all()
            bullets_active = [_bullet_to_dict(b) for b in all_bullets if b.is_active]
            bullets_discarded = [_discarded_to_dict(b) for b in all_bullets if not b.is_active]

        entity_name = (orch.kg_name if orch else None) or run.entity_id
        duration = None
        if run.process_completed_at_utc and run.process_started_at_utc:
            duration = int((run.process_completed_at_utc - run.process_started_at_utc).total_seconds())

    return {
        "run_id": run_id,
        "entity_id": run.entity_id,
        "entity_name": entity_name,
        "status": run.status,
        "started_at": _iso(run.process_started_at_utc),
        "completed_at": _iso(run.process_completed_at_utc),
        "duration_sec": duration,
        "window_start": _iso(run.report_window_start),
        "window_end": _iso(run.report_window_end),
        "bullets_saved": len(bullets_active),
        "bullets_discarded": len(bullets_discarded),
        "bullets": bullets_active,
        "discarded": bullets_discarded,
        "narrative": narrative.narrative_text if narrative else None,
        "error": run.error_summary[:200] if run.error_summary else None,
    }


@router.get("/run-data.json")
def get_run_data() -> dict:
    """Maps to window.RUN_DATA in the React frontend."""
    engine = get_engine()
    with Session(engine) as session:
        recent_runs_rows = session.exec(
            select(SQLEntityPipelineRunLog)
            .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
            .limit(7)
        ).all()
        # Resolve entity names
        orch_map = {
            r.entity_id: r for r in session.exec(select(SQLEntityOrchestrationState)).all()
        }
        recent = []
        for r in recent_runs_rows:
            orch = orch_map.get(r.entity_id)
            elapsed = 0
            if r.process_started_at_utc:
                end = r.process_completed_at_utc or datetime.now(timezone.utc)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                start = r.process_started_at_utc
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                elapsed = int((end - start).total_seconds())
            bullets = session.exec(
                select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == r.run_id)
            ).all()
            saved = sum(1 for b in bullets if b.is_active)
            discarded = sum(1 for b in bullets if not b.is_active)
            recent.append({
                "id": str(r.run_id)[:8],
                "entity": (orch.kg_name if orch else None) or r.entity_id,
                "ticker": (orch.kg_ticker if orch else None) or "",
                "status": r.status,
                "started": r.process_started_at_utc.strftime("%H:%M") if r.process_started_at_utc else "",
                "elapsed": elapsed,
                "saved": saved,
                "discarded": discarded,
                "error": r.error_summary[:80] if r.error_summary else None,
            })

    return {
        "recent": recent,
        "presetThemes": [
            "Strategic Partnerships",
            "Infrastructure Investment",
            "Financial Performance",
            "Product & Technology",
            "Operational Enhancements",
            "Regulatory & Legal",
            "Leadership & Governance",
            "Capital Markets",
        ],
        "sources": [
            {"id": "news", "label": "News", "checked": False},
            {"id": "news_premium", "label": "News (premium)", "checked": True},
            {"id": "filings", "label": "SEC filings", "checked": False},
            {"id": "transcripts", "label": "Transcripts", "checked": False},
        ],
        "models": [
            {"id": "fast", "label": "Fast", "desc": "gpt-4.1-mini · embed-3-large", "cost": 0.18, "time": "2–3m"},
            {"id": "balanced", "label": "Balanced", "desc": "gpt-4.1 + gpt-5-mini", "cost": 0.42, "time": "4–5m"},
            {"id": "thorough", "label": "Thorough", "desc": "gpt-4.1 + 2× novelty", "cost": 1.24, "time": "8–10m"},
        ],
        # Per-entity Compose cost estimates keyed by entity_id.
        # Each value: {"costDisplay": "$0.27"} — loaded from data/universe_entity_costs.csv.
        "composeEstimates": _load_compose_estimates(),
        # The live log + streamBullets are kept as a static sample for now —
        # streaming a real run's logs would need an SSE/WebSocket endpoint.
        "log": [],
        "streamBullets": [],
    }


# ── Entity history (Archive view) ────────────────────────────────────────────

@router.get("/entity/{entity_id}/history")
def get_entity_history(entity_id: str) -> dict:
    """Full run history for one entity — used by Archive view on company click."""
    engine = get_engine()
    with Session(engine) as session:
        history = _build_history(session, entity_id, limit=60)
        pulse = _build_pulse(session, entity_id)
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        kg = _parse_kg_payload(orch.kg_payload_json if orch else None)
        ticker = _TICKER_MAP.get(entity_id) or kg.get("ticker") or (orch.kg_ticker if orch else "") or ""
    return {
        "entityId": entity_id,
        "entityName": (orch.kg_name if orch else None) or entity_id,
        "ticker": ticker,
        "history": history,
        "pulse": pulse,
    }


# ── Forensics (History Details view) ─────────────────────────────────────────

@router.get("/entity/{entity_id}/forensics")
def get_entity_forensics(entity_id: str) -> dict:
    """Per-day rejection breakdown for one entity — used by Forensics view."""
    engine = get_engine()
    with Session(engine) as session:
        all_runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            )
            .order_by(desc(SQLEntityPipelineRunLog.report_window_end))
            .limit(30)
        ).all()

        # Build per-run dicts then group by UTC calendar date (preserving desc order).
        days_map: dict[str, list[dict]] = {}
        for r in all_runs:
            bullets = session.exec(
                select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == r.run_id)
            ).all()
            published = [_bullet_to_dict(b) for b in bullets if b.is_active]
            _, rejected_n, rejection_groups = _build_forensics_rejection_groups(engine, r.run_id)

            narrative = session.exec(
                select(SQLRunNarrative).where(SQLRunNarrative.run_id == r.run_id)
            ).first()
            date = r.report_window_end.date().isoformat() if r.report_window_end else ""
            run_dict = {
                "runId": str(r.run_id)[:8],
                "windowStart": _iso(r.report_window_start),
                "windowEnd": _iso(r.report_window_end),
                "published": len(published),
                "rejected": rejected_n,
                "narrative": narrative.narrative_text if narrative else None,
                "bullets": published,
                "rejectionGroups": rejection_groups,
            }
            if date not in days_map:
                days_map[date] = []
            days_map[date].append(run_dict)

        days = [{"date": date, "runs": runs} for date, runs in days_map.items()]

        orch = session.get(SQLEntityOrchestrationState, entity_id)
        kg = _parse_kg_payload(orch.kg_payload_json if orch else None)
        ticker = _TICKER_MAP.get(entity_id) or kg.get("ticker") or ""
    return {
        "entityId": entity_id,
        "entityName": (orch.kg_name if orch else None) or entity_id,
        "ticker": ticker,
        "days": days,
    }


# ── Brief "Read also" — other companies active on same day ───────────────────

@router.get("/brief/related")
def get_related_briefs(entity_id: str, date: str) -> dict:
    """Return other companies (ranked by bullets saved) active on the same calendar day."""
    engine = get_engine()
    try:
        from datetime import date as _date
        td = _date.fromisoformat(date)
    except ValueError:
        return {"related": []}

    day_start = datetime(td.year, td.month, td.day, 0, 0, 0)
    day_end = datetime(td.year, td.month, td.day, 23, 59, 59)

    with Session(engine) as session:
        runs = session.exec(
            select(SQLEntityPipelineRunLog).where(
                SQLEntityPipelineRunLog.entity_id != entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                SQLEntityPipelineRunLog.report_window_end >= day_start,
                SQLEntityPipelineRunLog.report_window_end <= day_end,
            )
        ).all()

        scored = []
        for r in runs:
            count = len(session.exec(
                select(SQLBulletRunLog).where(
                    SQLBulletRunLog.run_id == r.run_id,
                    SQLBulletRunLog.is_active == True,  # noqa: E712
                )
            ).all())
            if count > 0:
                orch = session.get(SQLEntityOrchestrationState, r.entity_id)
                ticker = _TICKER_MAP.get(r.entity_id) or (orch.kg_ticker if orch else "") or ""
                scored.append({
                    "entityId": r.entity_id,
                    "entityName": (orch.kg_name if orch else None) or r.entity_id,
                    "ticker": ticker,
                    "bulletsSaved": count,
                    "date": date,
                    "runId": str(r.run_id)[:8],
                })
        scored.sort(key=lambda x: x["bulletsSaved"], reverse=True)
    return {"related": scored[:25]}


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.post("/admin/reset")
def frontend_admin_reset() -> dict:
    """Reset the entire database (drops and recreates all tables)."""
    from bigdata_briefs.orchestration.db import ensure_orchestration_schema
    from sqlmodel import SQLModel
    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    ensure_orchestration_schema(engine)
    return {"status": "reset", "message": "Database reset successfully."}


@router.delete("/admin/entity/{entity_id}")
def frontend_admin_delete_entity(entity_id: str) -> dict:
    """Delete all data for a single entity."""
    from bigdata_briefs.api.routes.admin import _delete_entity_data
    engine = get_engine()
    _delete_entity_data(engine, entity_id)
    return {"status": "deleted", "entity_id": entity_id}


@router.get("/admin/stats")
def get_admin_stats() -> dict:
    """Real DB counts for the Admin view."""
    engine = get_engine()
    with Session(engine) as session:
        from bigdata_briefs.novelty.sql_models import SQLBulletPointEmbedding, SQLChunkTextHash
        runs = session.exec(select(SQLEntityPipelineRunLog)).all()
        bullets = session.exec(select(SQLBulletRunLog)).all()
        embeddings = session.exec(select(SQLBulletPointEmbedding)).all()
        chunks = session.exec(select(SQLChunkTextHash)).all()
    return {
        "totalRuns": len(runs),
        "succeededRuns": sum(1 for r in runs if r.status == "succeeded"),
        "failedRuns": sum(1 for r in runs if r.status == "failed"),
        "totalBullets": len(bullets),
        "activeBullets": sum(1 for b in bullets if b.is_active),
        "embeddings": len(embeddings),
        "chunkHashes": len(chunks),
    }


@router.get("/cost/runs-by-entity/{entity_id}")
def get_cost_runs_for_entity(entity_id: str, limit: int = 200) -> dict:
    """Runs for the Cost picker: reporting window date + total cost when metrics exist."""
    if limit < 1 or limit > 500:
        limit = 200
    engine = get_engine()
    with Session(engine) as session:
        runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(
                SQLEntityPipelineRunLog.entity_id == entity_id,
                SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
            )
            .order_by(desc(SQLEntityPipelineRunLog.report_window_end))
            .limit(limit)
        ).all()
        run_ids = [r.run_id for r in runs]
        metrics_by_run: dict[str, SQLRunMetrics] = {}
        if run_ids:
            for m in session.exec(
                select(SQLRunMetrics).where(SQLRunMetrics.run_id.in_(run_ids))
            ).all():
                metrics_by_run[str(m.run_id)] = m
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        entity_name = (orch.kg_name if orch else None) or entity_id
        ticker = _TICKER_MAP.get(entity_id) or (orch.kg_ticker if orch else "") or ""
        rows: list[dict] = []
        for r in runs:
            m = metrics_by_run.get(str(r.run_id))
            we = r.report_window_end.date().isoformat() if r.report_window_end else ""
            ws = r.report_window_start.date().isoformat() if r.report_window_start else ""
            rid8 = str(r.run_id)[:8]
            if not m:
                rows.append({
                    "runId": rid8,
                    "runIdFull": str(r.run_id),
                    "windowStart": ws,
                    "windowEnd": we,
                    "hasMetrics": False,
                    "cost": None,
                    "duration": "—",
                })
                continue
            total = (
                float(m.total_llm_cost_usd or 0)
                + float(m.total_embedding_cost_usd or 0)
                + float(calculate_chunk_cost(m.chunks_total or 0))
            )
            dur_s = 0
            if r.process_completed_at_utc and r.process_started_at_utc:
                dur_s = int((r.process_completed_at_utc - r.process_started_at_utc).total_seconds())
            rows.append({
                "runId": rid8,
                "runIdFull": str(r.run_id),
                "windowStart": ws,
                "windowEnd": we,
                "hasMetrics": True,
                "cost": round(total, 4),
                "duration": _fmt_duration(float(dur_s)),
            })
        return {
            "entityId": entity_id,
            "entityName": entity_name,
            "ticker": ticker,
            "runs": rows,
        }

@router.get("/cost/breakdown")
def get_cost_breakdown(run_id: str | None = None) -> dict:
    """Cost view: one run breakdown plus recent runs for the sidebar."""
    engine = get_engine()
    with Session(engine) as session:
        recent = _build_recent_runs_for_cost(session)
        run: SQLEntityPipelineRunLog | None = None
        if run_id:
            run = _find_run_by_id_fragment(session, run_id)
        if run is None:
            run = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).first()
        if run is None:
            return {"breakdown": None, "recentRuns": recent, "error": "no_run"}
        cost = _build_cost_dict_for_run(session, run)
        if cost is None:
            return {"breakdown": None, "recentRuns": recent, "error": "no_metrics"}
        cost["recentForBreakdown"] = recent
        return {"breakdown": cost, "recentRuns": recent}

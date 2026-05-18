"""Generate and store a portfolio narrative after a batch run completes."""

from __future__ import annotations

import json
from datetime import date as date_cls, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import desc
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs import logger

if TYPE_CHECKING:
    pass

_PORTFOLIO_BRIEF_TOP_N = 5

_METRIC_DB_FIELD = {
    "media_attention": ("chunks_ewm_short", "chunks_zscore_mo"),
    "sentiment":       ("sent_ewm_short",   "sent_zscore_mo"),
}


def _rank_from_db(session, entity_ids: list[str], metric: str) -> list[str]:
    """Rank entities by |Δ zscore| using the last 2 rows in SQLEntitySignalHistory.

    Reads directly from DB — no API calls. Called after compute_and_store_signals()
    has already populated the table for this batch.
    """
    from bigdata_briefs.orchestration.models import SQLEntitySignalHistory

    _, zscore_col = _METRIC_DB_FIELD.get(metric, ("chunks_ewm_short", "chunks_zscore_mo"))

    scores: dict[str, float] = {}
    for eid in entity_ids:
        rows = session.exec(
            select(SQLEntitySignalHistory)
            .where(SQLEntitySignalHistory.entity_id == eid)
            .order_by(desc(SQLEntitySignalHistory.date))
            .limit(2)
        ).all()
        if len(rows) < 2:
            scores[eid] = 0.0
            continue
        today_val     = getattr(rows[0], zscore_col, None)
        yesterday_val = getattr(rows[1], zscore_col, None)
        if today_val is None or yesterday_val is None:
            scores[eid] = 0.0
        else:
            scores[eid] = abs(float(today_val) - float(yesterday_val))

    return sorted(entity_ids, key=lambda e: scores.get(e, 0.0), reverse=True)


def generate_and_store_portfolio_brief(
    engine: Engine,
    date_iso: str,
    top_n: int = _PORTFOLIO_BRIEF_TOP_N,
    ranking_metric: str = "media_attention",
) -> None:
    """Generate a portfolio narrative for the top N companies on date_iso and persist it.

    Called in a daemon thread after batch/run-parallel completes. Silently swallowed
    on any error so it never affects the batch outcome.
    """
    from bigdata_briefs.orchestration.models import (
        SQLBulletRunLog,
        SQLEntityOrchestrationState,
        SQLEntityPipelineRunLog,
        SQLPortfolioBrief,
    )

    try:
        td = date_cls.fromisoformat(date_iso)
    except ValueError:
        return

    day_start = datetime(td.year, td.month, td.day, 0, 0, 0)
    day_end = datetime(td.year, td.month, td.day, 23, 59, 59)

    try:
        # ── collect active bullets per entity for this date ──────────────
        with Session(engine) as session:
            runs = session.exec(
                select(SQLEntityPipelineRunLog).where(
                    SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]),
                    SQLEntityPipelineRunLog.report_window_end >= day_start,
                    SQLEntityPipelineRunLog.report_window_end <= day_end,
                )
            ).all()

            entity_bullets: dict[str, list[str]] = {}
            for run in runs:
                bullets = session.exec(
                    select(SQLBulletRunLog).where(
                        SQLBulletRunLog.run_id == run.run_id,
                        SQLBulletRunLog.is_active == True,  # noqa: E712
                    )
                ).all()
                if bullets:
                    eid = run.entity_id
                    if eid not in entity_bullets:
                        entity_bullets[eid] = []
                    entity_bullets[eid].extend(b.text for b in bullets)

            if not entity_bullets:
                logger.info("Portfolio brief: no active bullets for date", date=date_iso)
                return

            # ── rank by signal delta from DB (no extra API calls) ────────
            all_entity_ids = list(entity_bullets.keys())
            try:
                signal_order = _rank_from_db(session, all_entity_ids, ranking_metric)
            except Exception as exc:
                logger.warning("DB signal ranking failed, falling back to bullet count", error=str(exc))
                signal_order = sorted(all_entity_ids, key=lambda e: len(entity_bullets.get(e, [])), reverse=True)

            # ── select top_n with fallback ────────────────────────────────
            # Prefer companies with active bullets; if < top_n found, fill from the rest
            with_bullets    = [e for e in signal_order if len(entity_bullets.get(e, [])) > 0]
            without_bullets = [e for e in signal_order if len(entity_bullets.get(e, [])) == 0]

            selected = with_bullets[:top_n]
            if len(selected) < top_n:
                selected += without_bullets[:top_n - len(selected)]

            ranked = [(eid, entity_bullets.get(eid, [])) for eid in selected]

            if not ranked:
                return

            # ── resolve metadata ─────────────────────────────────────────
            from bigdata_briefs.api.routes.frontend import _TICKER_MAP  # module-level map

            companies_out: list[dict] = []
            for eid, texts in ranked:
                orch = session.get(SQLEntityOrchestrationState, eid)
                name = (orch.kg_name if orch else None) or eid
                ticker = _TICKER_MAP.get(eid) or (orch.kg_ticker if orch else "") or ""
                companies_out.append({
                    "entityId": eid,
                    "name": name,
                    "ticker": ticker,
                    "bulletCount": len(texts),
                })

        # ── build narrative directly from bullet summaries (LLM disabled) ──
        sections = []
        for i, (eid, texts) in enumerate(ranked):
            if not texts:
                continue
            name   = companies_out[i]["name"]
            ticker = companies_out[i]["ticker"]
            joined = " ".join(f"{t.strip().rstrip('.')}." for t in texts)
            sections.append(f"{name}\n{joined}")

        narrative   = "\n\n".join(sections) if sections else None
        narrative_b = None

        if not narrative:
            return

        # ── persist (upsert: one row per date) ───────────────────────────
        with Session(engine) as session:
            existing = session.exec(
                select(SQLPortfolioBrief).where(SQLPortfolioBrief.date == date_iso)
            ).first()
            if existing:
                session.delete(existing)
                session.flush()
            session.add(SQLPortfolioBrief(
                date=date_iso,
                top_n=len(ranked),
                narrative=narrative,
                narrative_b=narrative_b,
                companies_json=json.dumps(companies_out),
                generated_at=datetime.now(timezone.utc),
            ))
            session.commit()

        logger.info(
            "Portfolio brief generated and stored",
            date=date_iso,
            companies=len(ranked),
        )

    except Exception:
        logger.exception("Portfolio brief generation failed", date=date_iso)

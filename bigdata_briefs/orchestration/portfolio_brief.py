"""Generate and store a portfolio narrative after a batch run completes."""

from __future__ import annotations

import json
from datetime import date as date_cls, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs import logger

if TYPE_CHECKING:
    pass

_PORTFOLIO_BRIEF_TOP_N = 5


def generate_and_store_portfolio_brief(
    engine: Engine,
    date_iso: str,
    top_n: int = _PORTFOLIO_BRIEF_TOP_N,
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

            # ── rank and clip ────────────────────────────────────────────
            ranked = [
                (eid, texts)
                for eid, texts in sorted(
                    entity_bullets.items(), key=lambda x: len(x[1]), reverse=True
                )
                if len(texts) > 0
            ][:top_n]

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

        # ── build prompt ──────────────────────────────────────────────────
        company_names = ", ".join(c["name"] for c in companies_out)
        sections = []
        for i, (eid, texts) in enumerate(ranked):
            name = companies_out[i]["name"]
            joined = " ".join(f"{t.strip().rstrip('.')}." for t in texts)
            sections.append(f"**{name}**: {joined}")
        briefing_text = "\n\n".join(sections)

        user_msg = (
            f"Portfolio brief for {date_iso}.\n\n"
            f"Companies covered: {company_names}\n\n"
            f"Briefing summaries:\n\n{briefing_text}\n\n"
            "Write a portfolio summary covering the key themes and developments "
            "across these companies today."
        )

        # ── call LLM ─────────────────────────────────────────────────────
        from bigdata_briefs.llm_client import LLMClient
        from pydantic import BaseModel as _BaseModel

        class _PortfolioBriefResponse(_BaseModel):
            narrative: str

        llm = LLMClient()
        response = llm.call_with_response_format(
            system=[{
                "role": "system",
                "content": (
                    "You are a financial analyst writing a concise portfolio brief. "
                    "Given the briefing sentences for multiple companies on a specific date, "
                    "write a fluent, concise portfolio summary (3-5 sentences) that synthesises "
                    "the most important developments across these companies. "
                    "Do not use bullet points. Write in third person. "
                    "Do not mention the word 'brief' or 'pipeline'. "
                    "Focus on material developments, themes, and notable events."
                ),
            }],
            messages=[{"role": "user", "content": user_msg}],
            model="gpt-4.1",
            max_tokens=200,
            text_format=_PortfolioBriefResponse,
            step_name="portfolio_brief",
        )
        if response is None or not response.narrative:
            return
        narrative = response.narrative.strip()

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

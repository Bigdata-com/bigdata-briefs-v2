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

        # ── build shared prompt context ───────────────────────────────────
        company_names = ", ".join(c["name"] for c in companies_out)
        sections = []
        for i, (eid, texts) in enumerate(ranked):
            name = companies_out[i]["name"]
            joined = " ".join(f"{t.strip().rstrip('.')}." for t in texts)
            sections.append(f"**{name}**: {joined}")
        briefing_text = "\n\n".join(sections)

        user_msg = (
            f"Date: {date_iso}.\n\n"
            f"Companies: {company_names}\n\n"
            f"Developments:\n\n{briefing_text}"
        )

        # ── two prompts ───────────────────────────────────────────────────
        _SYS_THEMATIC = (
            "You are a financial editor writing a concise morning portfolio note. "
            "Given the day's developments across multiple companies, identify 1-2 dominant "
            "cross-cutting themes and write 2-3 sentences about those themes, using the "
            "companies as examples rather than listing them one by one. "
            "Do not summarise each company sequentially. "
            "Write in third person. No bullet points. "
            "Do not use the words 'brief' or 'pipeline'."
        )

        _SYS_LEAD = (
            "You are a financial editor writing a concise morning portfolio note. "
            "Write exactly 2 sentences: the first captures the dominant theme of the day "
            "in one strong declarative sentence; the second cites 2-3 specific concrete "
            "developments that support it. "
            "Do not summarise each company separately or sequentially. "
            "Write in third person. No bullet points. "
            "Do not use the words 'brief' or 'pipeline'."
        )

        # ── call both LLMs in parallel ────────────────────────────────────
        from concurrent.futures import ThreadPoolExecutor as _TPE
        from bigdata_briefs.llm_client import LLMClient
        from pydantic import BaseModel as _BaseModel

        class _R(_BaseModel):
            narrative: str

        llm = LLMClient()

        def _call(sys_prompt, step):
            try:
                r = llm.call_with_response_format(
                    system=[{"role": "system", "content": sys_prompt}],
                    messages=[{"role": "user", "content": user_msg}],
                    model="gpt-4.1",
                    max_tokens=400,
                    text_format=_R,
                    step_name=step,
                )
                return r.narrative.strip() if r and r.narrative else None
            except Exception:
                return None

        with _TPE(max_workers=2) as ex:
            fut_a = ex.submit(_call, _SYS_THEMATIC, "portfolio_brief_thematic")
            fut_b = ex.submit(_call, _SYS_LEAD,     "portfolio_brief_lead")
            narrative   = fut_a.result()
            narrative_b = fut_b.result()

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

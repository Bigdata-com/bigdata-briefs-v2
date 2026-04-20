"""Query bullet rows for orchestrator CLI output (previous vs new, novelty-ok)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs.models import ReportDates
from bigdata_briefs.novelty.sql_models import SQLBulletPointEmbedding


def _novelty_ok_condition():
    return or_(
        SQLBulletPointEmbedding.novelty.is_(True),
        SQLBulletPointEmbedding.status.in_(["keep", "rewrite"]),
    )


def _exclude_novelty_search_mixed_rewrite_mirror():
    """
    Novelty-via-search ``mixed`` verdict persists two rows (pre-rewrite + canonical rewritten).

    Both pass ``_novelty_ok_condition``; for orchestrator output we keep one row per logical bullet
    (the canonical / report-facing row, or any legacy row without this field).
    """
    apf = SQLBulletPointEmbedding.added_past_evidence_from
    return or_(apf.is_(None), apf != "rewrite")


def fetch_previous_bullets(
    engine: Engine,
    entity_id: str,
    current_window: ReportDates,
) -> list[dict[str, Any]]:
    """
    Bullets from past orchestrated windows: ``report_window_end`` strictly before
    ``current_window.start``. Rows without ``report_window_end`` are excluded (no legacy
    ``date``-only heuristic).
    """
    start = current_window.start
    historical = and_(
        SQLBulletPointEmbedding.report_window_end.isnot(None),
        SQLBulletPointEmbedding.report_window_end <= start,
    )
    with Session(engine) as session:
        rows = session.exec(
            select(SQLBulletPointEmbedding)
            .where(SQLBulletPointEmbedding.entity_id == entity_id)
            .where(historical)
            .order_by(SQLBulletPointEmbedding.date, SQLBulletPointEmbedding.id)
        ).all()
    return [_bullet_row_to_output(r) for r in rows]


def fetch_new_novelty_ok_bullets(
    engine: Engine,
    entity_id: str,
    current_window: ReportDates,
) -> list[dict[str, Any]]:
    """Bullets written for this report window that passed novelty (keep/rewrite / novelty True).

    Excludes the novelty-search ``mixed`` pre-rewrite mirror row (``added_past_evidence_from ==
    \"rewrite\"``) so counts match one row per LangGraph verdict (novel + mixed), not two per mixed.
    """
    ws, we = current_window.start, current_window.end
    window_match = and_(
        SQLBulletPointEmbedding.report_window_start.isnot(None),
        SQLBulletPointEmbedding.report_window_end.isnot(None),
        SQLBulletPointEmbedding.report_window_start == ws,
        SQLBulletPointEmbedding.report_window_end == we,
    )
    with Session(engine) as session:
        rows = session.exec(
            select(SQLBulletPointEmbedding)
            .where(SQLBulletPointEmbedding.entity_id == entity_id)
            .where(window_match)
            .where(_novelty_ok_condition())
            .where(_exclude_novelty_search_mixed_rewrite_mirror())
            .order_by(SQLBulletPointEmbedding.date, SQLBulletPointEmbedding.id)
        ).all()
    return [_bullet_row_to_output(r) for r in rows]


def _bullet_row_to_output(r: SQLBulletPointEmbedding) -> dict[str, Any]:
    rw_s = getattr(r, "report_window_start", None)
    rw_e = getattr(r, "report_window_end", None)
    return {
        "id": str(r.id),
        "entity_id": r.entity_id,
        "date": r.date.isoformat() if r.date else None,
        "original_text": r.original_text,
        "status": r.status,
        "novelty": r.novelty,
        "report_window_start": rw_s.isoformat() if rw_s else None,
        "report_window_end": rw_e.isoformat() if rw_e else None,
    }

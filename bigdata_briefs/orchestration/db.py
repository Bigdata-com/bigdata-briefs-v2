"""SQLite schema helpers for orchestration and bullet report-window columns."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from bigdata_briefs import logger

# Register table models on SQLModel.metadata
from bigdata_briefs.novelty import sql_models as _novelty_sql  # noqa: F401
from bigdata_briefs.novelty import sql_pipeline_checkpoint as _novelty_cp  # noqa: F401
from bigdata_briefs.orchestration import models as _orch_models  # noqa: F401


def ensure_orchestration_schema(engine: Engine) -> None:
    """Create orchestration and novelty tables if missing (idempotent)."""
    SQLModel.metadata.create_all(engine)
    _ensure_report_window_columns(engine)
    _ensure_is_fully_novel_columns(engine)
    _ensure_bullet_run_log_json_columns(engine)
    _ensure_run_metrics_columns(engine)
    _ensure_signal_history_columns(engine)


def _ensure_bullet_run_log_json_columns(engine: Engine) -> None:
    """Add JSON display columns to sqlbulletrunlog for existing DBs."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sqlbulletrunlog)")).fetchall()
    if not rows:
        return  # table doesn't exist yet; create_all will handle it
    colnames = {r[1] for r in rows}
    new_cols = {
        "citations_json": "TEXT NOT NULL DEFAULT '[]'",
        "evaluator_details_json": "TEXT NOT NULL DEFAULT '[]'",
        "claim_verdicts_json": "TEXT NOT NULL DEFAULT '[]'",
        "evidence_map_json": "TEXT NOT NULL DEFAULT '{}'",
        "grounding_citations_json": "TEXT NOT NULL DEFAULT '[]'",
        "search_relevance_score": "INTEGER",
        "search_relevance_reason": "TEXT",
    }
    for col, definition in new_cols.items():
        if col not in colnames:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE sqlbulletrunlog ADD COLUMN {col} {definition}"))
                conn.commit()


def _ensure_report_window_columns(engine: Engine) -> None:
    """Add report_window_* to sqlbulletpointembedding when upgrading existing DBs."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sqlbulletpointembedding)")).fetchall()
    colnames = {r[1] for r in rows}
    if "report_window_start" not in colnames:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE sqlbulletpointembedding "
                    "ADD COLUMN report_window_start TIMESTAMP"
                )
            )
            conn.commit()
    if "report_window_end" not in colnames:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE sqlbulletpointembedding "
                    "ADD COLUMN report_window_end TIMESTAMP"
                )
            )
            conn.commit()


def _ensure_run_metrics_columns(engine: Engine) -> None:
    """Add new columns to sqlrunmetrics for existing DBs."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sqlrunmetrics)")).fetchall()
    if not rows:
        return
    colnames = {r[1] for r in rows}
    if "sources_scanned" not in colnames:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE sqlrunmetrics ADD COLUMN sources_scanned INTEGER NOT NULL DEFAULT 0"))
            conn.commit()


def _ensure_signal_history_columns(engine: Engine) -> None:
    """Add new signal metric columns to sqlentitysignalhistory for existing DBs."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sqlentitysignalhistory)")).fetchall()
    if not rows:
        return
    colnames = {r[1] for r in rows}
    new_cols = {
        "chunks_zscore_qt": "REAL",
        "sent_zscore_qt": "REAL",
        "sent_ewm_long": "REAL",
        "sent_momentum": "REAL",
        "chunks_momentum_pct": "REAL",
    }
    for col, definition in new_cols.items():
        if col not in colnames:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE sqlentitysignalhistory ADD COLUMN {col} {definition}"))
                conn.commit()


def _ensure_is_fully_novel_columns(engine: Engine) -> None:
    """Ensure the ``is_fully_novel`` column on generated_bullet_points and sqlbulletrunlog.

    Upgrades both legacy schemas with no value change (the flag's true/false meaning
    is identical across all three names):

    - DBs with the older ``is_novel`` column are renamed in place to
      ``is_fully_novel`` (same values).
    - Much older DBs with only ``not_fully_novel`` get ``is_fully_novel`` added and
      backfilled with the inverted value (``is_fully_novel = NOT not_fully_novel``).
    - Fresh tables get the column added (default 1 = fully novel).

    Any leftover legacy columns (``is_novel``, ``not_fully_novel``) are dropped so
    their NOT NULL constraints don't break later inserts.
    """
    for table in ("generated_bullet_points", "sqlbulletrunlog"):
        with engine.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        if not rows:
            continue  # table doesn't exist yet; create_all handles fresh schema
        colnames = {r[1] for r in rows}

        if "is_fully_novel" not in colnames:
            with engine.connect() as conn:
                if "is_novel" in colnames:
                    # Same semantics and values — rename the column in place.
                    conn.execute(
                        text(f"ALTER TABLE {table} RENAME COLUMN is_novel TO is_fully_novel")
                    )
                else:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN is_fully_novel BOOLEAN NOT NULL DEFAULT 1")
                    )
                    if "not_fully_novel" in colnames:
                        conn.execute(text(f"UPDATE {table} SET is_fully_novel = NOT not_fully_novel"))
                conn.commit()
            # Refresh after a possible rename so the drop step below is accurate.
            with engine.connect() as conn:
                colnames = {
                    r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                }

        # Drop any leftover legacy columns. The renamed models no longer write them,
        # so a NOT NULL constraint would make every INSERT fail.
        for legacy in ("not_fully_novel", "is_novel"):
            if legacy in colnames:
                try:
                    with engine.connect() as conn:
                        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {legacy}"))
                        conn.commit()
                except Exception as e:  # noqa: BLE001 — older SQLite without DROP COLUMN
                    logger.warning(
                        "Could not drop legacy %s from %s: %s", legacy, table, e
                    )

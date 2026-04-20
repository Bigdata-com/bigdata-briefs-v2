"""SQLite schema helpers for orchestration and bullet report-window columns."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

# Register table models on SQLModel.metadata
from bigdata_briefs.novelty import sql_models as _novelty_sql  # noqa: F401
from bigdata_briefs.novelty import sql_pipeline_checkpoint as _novelty_cp  # noqa: F401
from bigdata_briefs.orchestration import models as _orch_models  # noqa: F401


def ensure_orchestration_schema(engine: Engine) -> None:
    """Create orchestration and novelty tables if missing (idempotent)."""
    SQLModel.metadata.create_all(engine)
    _ensure_report_window_columns(engine)
    _ensure_not_fully_novel_column(engine)


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


def _ensure_not_fully_novel_column(engine: Engine) -> None:
    """Add not_fully_novel to generated_bullet_points when upgrading existing DBs."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(generated_bullet_points)")).fetchall()
    colnames = {r[1] for r in rows}
    if "not_fully_novel" not in colnames:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE generated_bullet_points "
                    "ADD COLUMN not_fully_novel BOOLEAN NOT NULL DEFAULT 0"
                )
            )
            conn.commit()

"""Verify that _delete_date_data removes ALL run data for the given calendar
date and leaves data for other dates untouched."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.novelty.sql_models import (
    SQLBulletPointEmbedding,
    SQLChunkTextHash,
    SQLGeneratedBulletPoint,
)
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint
from bigdata_briefs.novelty.sql_step_wall_timing import SQLPipelineStepWallTiming
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityPipelineRunLog,
    SQLEntitySignalHistory,
    SQLPortfolioBrief,
    SQLRunMetrics,
    SQLRunNarrative,
)
from bigdata_briefs.api.routes.ui import _delete_date_data

# ── Dates under test ─────────────────────────────────────────────────────────

TARGET_DATE  = "2026-05-15"
OTHER_DATE   = "2026-05-16"
TARGET_END   = datetime(2026, 5, 15, 8, 0, 0, tzinfo=timezone.utc)
TARGET_START = datetime(2026, 5, 12, 8, 0, 0, tzinfo=timezone.utc)
OTHER_END    = datetime(2026, 5, 16, 8, 0, 0, tzinfo=timezone.utc)
OTHER_START  = datetime(2026, 5, 13, 8, 0, 0, tzinfo=timezone.utc)
NOW          = datetime(2026, 5, 15, 8, 0, 1, tzinfo=timezone.utc)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


# ── Seed helper ───────────────────────────────────────────────────────────────

def _seed(engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one row per table for TARGET_DATE and OTHER_DATE.
    Returns (target_run_id, other_run_id)."""

    def make_run(ws, we):
        return SQLEntityPipelineRunLog(
            entity_id="ENT1",
            report_window_start=ws,
            report_window_end=we,
            process_started_at_utc=we,
            status="succeeded",
        )

    target_run = make_run(TARGET_START, TARGET_END)
    other_run  = make_run(OTHER_START,  OTHER_END)

    with Session(engine) as s:
        s.add(target_run)
        s.add(other_run)
        s.flush()
        tid, oid = target_run.run_id, other_run.run_id

        for run_id in (tid, oid):
            s.add(SQLBulletRunLog(
                run_id=run_id, entity_id="ENT1", trace_id=str(uuid.uuid4()),
                is_active=True, text="bullet", created_at=NOW,
            ))

        for run_id, ws, we in ((tid, TARGET_START, TARGET_END), (oid, OTHER_START, OTHER_END)):
            s.add(SQLRunMetrics(
                run_id=run_id, entity_id="ENT1",
                report_window_start=ws, report_window_end=we, created_at=NOW,
            ))
            s.add(SQLRunNarrative(
                run_id=run_id, entity_id="ENT1",
                report_date=we, narrative_text="n", bullets_count=1, created_at=NOW,
            ))
            s.add(SQLGeneratedBulletPoint(
                run_id=str(run_id), entity_id="ENT1", entity_name="Ent",
                report_window_start=ws, report_window_end=we,
                created_at=NOW, trace_id=str(uuid.uuid4()), text="t",
            ))

        s.add(SQLBulletPointEmbedding(entity_id="ENT1", date=TARGET_END, original_text="t"))
        s.add(SQLBulletPointEmbedding(entity_id="ENT1", date=OTHER_END,  original_text="t"))

        s.add(SQLChunkTextHash(entity_id="ENT1", date=TARGET_END, text_hash="a" * 64, chunk_key="k1"))
        s.add(SQLChunkTextHash(entity_id="ENT1", date=OTHER_END,  text_hash="b" * 64, chunk_key="k2"))

        s.add(SQLBulletPipelineCheckpoint(
            bullet_trace_id=uuid.uuid4(), entity_id="ENT1", report_date=TARGET_END,
        ))
        s.add(SQLBulletPipelineCheckpoint(
            bullet_trace_id=uuid.uuid4(), entity_id="ENT1", report_date=OTHER_END,
        ))

        for day in (TARGET_DATE, OTHER_DATE):
            s.add(SQLPipelineStepWallTiming(
                request_id=str(uuid.uuid4()), entity_id="ENT1",
                calendar_day=day, pipeline_step="step1",
                started_at_utc=NOW, ended_at_utc=NOW, duration_seconds=1.0,
            ))

        s.add(SQLEntitySignalHistory(entity_id="ENT1", date=TARGET_DATE))
        s.add(SQLEntitySignalHistory(entity_id="ENT1", date=OTHER_DATE))

        s.add(SQLPortfolioBrief(date=TARGET_DATE, narrative="n", generated_at=NOW))
        s.add(SQLPortfolioBrief(date=OTHER_DATE,  narrative="n", generated_at=NOW))

        s.commit()

    return tid, oid


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_delete_date_removes_all_target_data(engine):
    target_run_id, other_run_id = _seed(engine)

    run_count = _delete_date_data(engine, TARGET_DATE)
    assert run_count == 1

    with Session(engine) as s:
        def count(model, *filters):
            return len(s.exec(select(model).where(*filters)).all())

        # ── All TARGET_DATE data must be gone ────────────────────────────────
        assert count(SQLEntityPipelineRunLog,
                     SQLEntityPipelineRunLog.run_id == target_run_id) == 0, \
            "SQLEntityPipelineRunLog not deleted"

        assert count(SQLBulletRunLog,
                     SQLBulletRunLog.run_id == target_run_id) == 0, \
            "SQLBulletRunLog not deleted"

        assert count(SQLRunMetrics,
                     SQLRunMetrics.run_id == target_run_id) == 0, \
            "SQLRunMetrics not deleted"

        assert count(SQLRunNarrative,
                     SQLRunNarrative.run_id == target_run_id) == 0, \
            "SQLRunNarrative not deleted"

        assert count(SQLGeneratedBulletPoint,
                     SQLGeneratedBulletPoint.run_id == str(target_run_id)) == 0, \
            "SQLGeneratedBulletPoint not deleted"

        assert count(SQLBulletPointEmbedding,
                     SQLBulletPointEmbedding.date == TARGET_END) == 0, \
            "SQLBulletPointEmbedding not deleted"

        assert count(SQLChunkTextHash,
                     SQLChunkTextHash.date == TARGET_END) == 0, \
            "SQLChunkTextHash not deleted"

        assert count(SQLBulletPipelineCheckpoint,
                     SQLBulletPipelineCheckpoint.report_date == TARGET_END) == 0, \
            "SQLBulletPipelineCheckpoint not deleted"

        assert count(SQLPipelineStepWallTiming,
                     SQLPipelineStepWallTiming.calendar_day == TARGET_DATE) == 0, \
            "SQLPipelineStepWallTiming not deleted"

        assert count(SQLEntitySignalHistory,
                     SQLEntitySignalHistory.date == TARGET_DATE) == 0, \
            "SQLEntitySignalHistory not deleted"

        assert count(SQLPortfolioBrief,
                     SQLPortfolioBrief.date == TARGET_DATE) == 0, \
            "SQLPortfolioBrief not deleted"

        # ── All OTHER_DATE data must survive ─────────────────────────────────
        assert count(SQLEntityPipelineRunLog,
                     SQLEntityPipelineRunLog.run_id == other_run_id) == 1, \
            "SQLEntityPipelineRunLog for other date was incorrectly deleted"

        assert count(SQLBulletRunLog,
                     SQLBulletRunLog.run_id == other_run_id) == 1, \
            "SQLBulletRunLog for other date was incorrectly deleted"

        assert count(SQLRunMetrics,
                     SQLRunMetrics.run_id == other_run_id) == 1, \
            "SQLRunMetrics for other date was incorrectly deleted"

        assert count(SQLRunNarrative,
                     SQLRunNarrative.run_id == other_run_id) == 1, \
            "SQLRunNarrative for other date was incorrectly deleted"

        assert count(SQLGeneratedBulletPoint,
                     SQLGeneratedBulletPoint.run_id == str(other_run_id)) == 1, \
            "SQLGeneratedBulletPoint for other date was incorrectly deleted"

        assert count(SQLBulletPointEmbedding,
                     SQLBulletPointEmbedding.date == OTHER_END) == 1, \
            "SQLBulletPointEmbedding for other date was incorrectly deleted"

        assert count(SQLChunkTextHash,
                     SQLChunkTextHash.date == OTHER_END) == 1, \
            "SQLChunkTextHash for other date was incorrectly deleted"

        assert count(SQLBulletPipelineCheckpoint,
                     SQLBulletPipelineCheckpoint.report_date == OTHER_END) == 1, \
            "SQLBulletPipelineCheckpoint for other date was incorrectly deleted"

        assert count(SQLPipelineStepWallTiming,
                     SQLPipelineStepWallTiming.calendar_day == OTHER_DATE) == 1, \
            "SQLPipelineStepWallTiming for other date was incorrectly deleted"

        assert count(SQLEntitySignalHistory,
                     SQLEntitySignalHistory.date == OTHER_DATE) == 1, \
            "SQLEntitySignalHistory for other date was incorrectly deleted"

        assert count(SQLPortfolioBrief,
                     SQLPortfolioBrief.date == OTHER_DATE) == 1, \
            "SQLPortfolioBrief for other date was incorrectly deleted"


def test_delete_date_invalid_format_raises(engine):
    with pytest.raises(ValueError):
        _delete_date_data(engine, "not-a-date")


def test_delete_date_empty_date_raises(engine):
    with pytest.raises(ValueError):
        _delete_date_data(engine, "")


def test_delete_date_no_data_is_safe(engine):
    """Calling delete on a date with no data should succeed and return 0 runs."""
    run_count = _delete_date_data(engine, "2020-01-01")
    assert run_count == 0

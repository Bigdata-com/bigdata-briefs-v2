"""Tests for rolling retention prune of live SQLite history."""

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
from bigdata_briefs.orchestration.retention import prune_live_database, retention_cutoff

OLD_DATE = "2026-04-01"
KEEP_DATE = "2026-08-20"
OLD_END = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)
KEEP_END = datetime(2026, 8, 20, 8, 0, 0, tzinfo=timezone.utc)
OLD_START = datetime(2026, 3, 29, 8, 0, 0, tzinfo=timezone.utc)
KEEP_START = datetime(2026, 8, 17, 8, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


def _seed(engine) -> tuple[uuid.UUID, uuid.UUID]:
    def make_run(ws: datetime, we: datetime) -> SQLEntityPipelineRunLog:
        return SQLEntityPipelineRunLog(
            entity_id="ENT1",
            report_window_start=ws,
            report_window_end=we,
            process_started_at_utc=we,
            status="succeeded",
        )

    old_run = make_run(OLD_START, OLD_END)
    keep_run = make_run(KEEP_START, KEEP_END)

    with Session(engine) as s:
        s.add(old_run)
        s.add(keep_run)
        s.flush()
        oid, kid = old_run.run_id, keep_run.run_id

        for run_id in (oid, kid):
            s.add(
                SQLBulletRunLog(
                    run_id=run_id,
                    entity_id="ENT1",
                    trace_id=str(uuid.uuid4()),
                    is_active=True,
                    text="bullet",
                    created_at=NOW,
                )
            )

        for run_id, ws, we in ((oid, OLD_START, OLD_END), (kid, KEEP_START, KEEP_END)):
            s.add(
                SQLRunMetrics(
                    run_id=run_id,
                    entity_id="ENT1",
                    report_window_start=ws,
                    report_window_end=we,
                    created_at=NOW,
                )
            )
            s.add(
                SQLRunNarrative(
                    run_id=run_id,
                    entity_id="ENT1",
                    report_date=we,
                    narrative_text="n",
                    bullets_count=1,
                    created_at=NOW,
                )
            )
            s.add(
                SQLGeneratedBulletPoint(
                    run_id=str(run_id),
                    entity_id="ENT1",
                    entity_name="Ent",
                    report_window_start=ws,
                    report_window_end=we,
                    created_at=NOW,
                    trace_id=str(uuid.uuid4()),
                    text="t",
                )
            )

        s.add(SQLBulletPointEmbedding(entity_id="ENT1", date=OLD_END, original_text="t"))
        s.add(SQLBulletPointEmbedding(entity_id="ENT1", date=KEEP_END, original_text="t"))
        s.add(
            SQLChunkTextHash(
                entity_id="ENT1", date=OLD_END, text_hash="a" * 64, chunk_key="k1"
            )
        )
        s.add(
            SQLChunkTextHash(
                entity_id="ENT1", date=KEEP_END, text_hash="b" * 64, chunk_key="k2"
            )
        )
        s.add(
            SQLBulletPipelineCheckpoint(
                bullet_trace_id=uuid.uuid4(), entity_id="ENT1", report_date=OLD_END
            )
        )
        s.add(
            SQLBulletPipelineCheckpoint(
                bullet_trace_id=uuid.uuid4(), entity_id="ENT1", report_date=KEEP_END
            )
        )
        for day in (OLD_DATE, KEEP_DATE):
            s.add(
                SQLPipelineStepWallTiming(
                    request_id=str(uuid.uuid4()),
                    entity_id="ENT1",
                    calendar_day=day,
                    pipeline_step="step1",
                    started_at_utc=NOW,
                    ended_at_utc=NOW,
                    duration_seconds=1.0,
                )
            )
            s.add(SQLEntitySignalHistory(entity_id="ENT1", date=day))
            s.add(SQLPortfolioBrief(date=day, narrative="n", generated_at=NOW))

        s.commit()

    return oid, kid


def test_retention_cutoff_aligns_to_utc_day() -> None:
    now = datetime(2026, 9, 8, 15, 30, 0, tzinfo=timezone.utc)
    assert retention_cutoff(keep_days=30, now=now) == datetime(
        2026, 8, 9, 0, 0, 0, tzinfo=timezone.utc
    )


def test_dry_run_does_not_delete(engine) -> None:
    _seed(engine)
    result = prune_live_database(engine, keep_days=30, dry_run=True, now=NOW)
    assert result.dry_run is True
    assert result.deleted["sqlentitypipelinerunlog"] == 1
    assert result.deleted["sqlbulletpointembedding"] == 1
    with Session(engine) as s:
        assert len(s.exec(select(SQLEntityPipelineRunLog)).all()) == 2
        assert len(s.exec(select(SQLBulletPointEmbedding)).all()) == 2
        assert len(s.exec(select(SQLPortfolioBrief)).all()) == 2


def test_execute_prunes_old_keeps_recent_and_portfolio(engine) -> None:
    old_id, keep_id = _seed(engine)
    result = prune_live_database(
        engine, keep_days=30, dry_run=False, vacuum=False, now=NOW
    )
    assert result.dry_run is False
    assert result.total_deleted() > 0

    with Session(engine) as s:
        runs = s.exec(select(SQLEntityPipelineRunLog)).all()
        assert [r.run_id for r in runs] == [keep_id]
        assert s.get(SQLEntityPipelineRunLog, old_id) is None

        assert len(s.exec(select(SQLBulletPointEmbedding)).all()) == 1
        assert len(s.exec(select(SQLChunkTextHash)).all()) == 1
        assert len(s.exec(select(SQLBulletPipelineCheckpoint)).all()) == 1
        assert len(s.exec(select(SQLBulletRunLog)).all()) == 1
        assert len(s.exec(select(SQLRunMetrics)).all()) == 1
        assert len(s.exec(select(SQLRunNarrative)).all()) == 1
        assert len(s.exec(select(SQLGeneratedBulletPoint)).all()) == 1
        assert len(s.exec(select(SQLEntitySignalHistory)).all()) == 1
        assert len(s.exec(select(SQLPipelineStepWallTiming)).all()) == 1
        # Portfolio briefs are intentionally retained.
        assert len(s.exec(select(SQLPortfolioBrief)).all()) == 2

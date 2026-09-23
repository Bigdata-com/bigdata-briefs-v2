"""Tests for rolling retention prune of live SQLite history."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
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
from bigdata_briefs.orchestration.retention import (
    default_keep_days,
    prune_live_database,
    retention_cutoff,
)
from bigdata_briefs.settings import settings

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


def test_default_keep_days_reads_the_setting(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RETENTION_KEEP_DAYS", 45)
    monkeypatch.setattr(settings, "NOVELTY_LOOKBACK_DAYS", 30)
    assert default_keep_days() == 45


def test_default_keep_days_never_cuts_into_novelty_history(monkeypatch) -> None:
    """A keep_days below the novelty lookback is raised, not honoured.

    Pruning inside the lookback makes already-published bullets look new again
    and nothing raises, so the floor is the only thing that catches it.
    """
    monkeypatch.setattr(settings, "RETENTION_KEEP_DAYS", 7)
    monkeypatch.setattr(settings, "NOVELTY_LOOKBACK_DAYS", 30)
    assert default_keep_days() == 30


def test_execute_truncates_the_wal(tmp_path) -> None:
    """A real prune leaves no -wal file behind: deleting rows alone frees no disk."""
    db = tmp_path / "wal.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
    SQLModel.metadata.create_all(eng)
    _seed(eng)
    assert db.with_name(db.name + "-wal").stat().st_size > 0

    prune_live_database(eng, keep_days=30, dry_run=False, now=NOW)

    assert db.with_name(db.name + "-wal").stat().st_size == 0

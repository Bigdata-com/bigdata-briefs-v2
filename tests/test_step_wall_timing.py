"""Pipeline step wall timings (JSON + SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.novelty.sql_step_wall_timing import (
    SQLPipelineStepWallTiming,
    flush_step_wall_timings_to_sqlite,
    persist_step_wall_timings,
)
from bigdata_briefs.novelty.wall_timing import (
    NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION,
    track_novelty_wall_substep,
)


@pytest.fixture
def wall_engine():
    engine = create_engine("sqlite:///:memory:")
    _ = SQLPipelineStepWallTiming
    SQLModel.metadata.create_all(engine)
    return engine


def test_entity_step_metrics_pipeline_wall_record() -> None:
    m = EntityStepMetrics("Acme")
    t0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=10)
    m.record_pipeline_step_wall("bullet_generation", t0, t1)
    rows = m.get_step_wall_timings()
    assert len(rows) == 1
    assert rows[0]["pipeline_step"] == "bullet_generation"
    assert rows[0]["substep"] is None
    assert rows[0]["duration_seconds"] == 10.0


def test_novelty_substep_accumulates() -> None:
    m = EntityStepMetrics("Acme")
    t0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    m.accumulate_novelty_substep_wall(
        NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION, t0, t0 + timedelta(seconds=2)
    )
    m.accumulate_novelty_substep_wall(
        NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION, t0 + timedelta(seconds=5), t0 + timedelta(seconds=6)
    )
    rows = m.get_step_wall_timings()
    assert len(rows) == 1
    assert rows[0]["pipeline_step"] == "novelty_check"
    assert rows[0]["substep"] == NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION
    assert rows[0]["duration_seconds"] == pytest.approx(3.0)


def test_track_novelty_wall_substep_context_none_metrics() -> None:
    with track_novelty_wall_substep(None, NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION):
        pass


def test_persist_step_wall_timings_roundtrip(wall_engine) -> None:
    rid = "550e8400-e29b-41d4-a716-446655440000"
    row = SQLPipelineStepWallTiming(
        request_id=rid,
        entity_id="E1",
        entity_name="Acme",
        calendar_day="2026-01-15",
        pipeline_step="initial_check",
        substep=None,
        started_at_utc=datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        ended_at_utc=datetime(2026, 1, 15, 10, 0, 5, tzinfo=timezone.utc),
        duration_seconds=5.0,
    )
    persist_step_wall_timings(wall_engine, [row])
    with Session(wall_engine) as session:
        found = session.exec(select(SQLPipelineStepWallTiming)).all()
    assert len(found) == 1
    assert found[0].pipeline_step == "initial_check"
    assert found[0].duration_seconds == 5.0


def test_flush_from_entity_metrics(wall_engine) -> None:
    m = EntityStepMetrics("Acme")
    t0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    m.record_pipeline_step_wall("exploratory_search", t0, t0 + timedelta(seconds=1.5))
    flush_step_wall_timings_to_sqlite(
        wall_engine,
        m,
        request_id="req-1",
        entity_id="E1",
        entity_name="Acme",
        calendar_day="2026-01-15",
    )
    with Session(wall_engine) as session:
        n = len(session.exec(select(SQLPipelineStepWallTiming)).all())
    assert n == 1

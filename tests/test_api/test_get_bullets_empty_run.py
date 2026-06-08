"""Tests for get_bullets correctly handling runs with 0 active bullets.

Bug: when a run produces 0 active bullets (all discarded), nothing is written to
SQLGeneratedBulletPoint. get_bullets with max_runs=1 would then return stale data
from the previous run instead of showing the correct empty result.

Fix: _get_empty_run_results_for_entity finds runs in SQLEntityPipelineRunLog that
have no active bullets in SQLBulletRunLog, and get_bullets merges them into the
result list before applying max_runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from bigdata_briefs.api.routes.reports import (
    _get_empty_run_results_for_entity,
    get_bullets,
)
from bigdata_briefs.api.schemas import BatchBulletsRequest
from bigdata_briefs.novelty.sql_models import SQLGeneratedBulletPoint
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_run_log(
    session: Session,
    entity_id: str,
    run_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    completed_at: datetime,
    status: str = "succeeded",
) -> SQLEntityPipelineRunLog:
    row = SQLEntityPipelineRunLog(
        run_id=run_id,
        entity_id=entity_id,
        report_window_start=window_start,
        report_window_end=window_end,
        process_started_at_utc=window_start,
        process_completed_at_utc=completed_at,
        status=status,
    )
    session.add(row)
    session.commit()
    return row


def _make_bullet_log(
    session: Session,
    run_id: uuid.UUID,
    entity_id: str,
    trace_id: str,
    is_active: bool,
    discard_stage: str | None = None,
    text: str = "some bullet",
) -> None:
    session.add(SQLBulletRunLog(
        run_id=run_id,
        entity_id=entity_id,
        trace_id=trace_id,
        is_active=is_active,
        discard_stage=discard_stage,
        text=text,
        created_at=datetime.now(timezone.utc),
    ))
    session.commit()


def _make_generated_bullet(
    session: Session,
    entity_id: str,
    entity_name: str,
    window_start: datetime,
    window_end: datetime,
    created_at: datetime,
    text: str = "active bullet",
) -> None:
    session.add(SQLGeneratedBulletPoint(
        run_id=str(uuid.uuid4()),
        entity_id=entity_id,
        entity_name=entity_name,
        report_window_start=window_start,
        report_window_end=window_end,
        created_at=created_at,
        trace_id=str(uuid.uuid4()),
        text=text,
        citations=None,
    ))
    session.commit()


# ── _get_empty_run_results_for_entity ─────────────────────────────────────────


def test_helper_returns_empty_when_all_runs_have_active_bullets(engine):
    """If every recent run produced active bullets, the helper returns nothing."""
    run_id = uuid.uuid4()
    with Session(engine) as s:
        _make_run_log(s, "E1", run_id, _ts(7), _ts(8), _ts(8, 13))
        _make_bullet_log(s, run_id, "E1", "t1", is_active=True)

    result = _get_empty_run_results_for_entity(engine, "E1", limit=5)

    assert result == []


def test_helper_finds_run_with_all_bullets_discarded(engine):
    """A run where all bullets were discarded appears as an empty RunBulletsResult."""
    run_id = uuid.uuid4()
    ws, we = _ts(7), _ts(8)
    with Session(engine) as s:
        _make_run_log(s, "E1", run_id, ws, we, _ts(8, 13))
        _make_bullet_log(s, run_id, "E1", "t1", is_active=False, discard_stage="relevance_score", text="off-topic")
        _make_bullet_log(s, run_id, "E1", "t2", is_active=False, discard_stage="novelty_embedding", text="old news")

    result = _get_empty_run_results_for_entity(engine, "E1", limit=5)

    assert len(result) == 1
    r = result[0]
    assert r.bullet_count == 0
    assert r.bullets_saved == 0
    assert r.bullets_discarded == 2
    assert r.discarded_by_relevance == ["off-topic"]
    assert r.discarded_by_novelty == ["old news"]
    assert r.report_window_start == ws.replace(tzinfo=None)
    assert r.report_window_end == we.replace(tzinfo=None)


def test_helper_finds_run_with_no_bullets_at_all(engine):
    """A run where the pipeline found no news at all (no SQLBulletRunLog rows)."""
    run_id = uuid.uuid4()
    ws, we = _ts(7), _ts(8)
    with Session(engine) as s:
        _make_run_log(s, "E1", run_id, ws, we, _ts(8, 13), status="no_data")

    result = _get_empty_run_results_for_entity(engine, "E1", limit=5)

    assert len(result) == 1
    assert result[0].bullet_count == 0
    assert result[0].bullets_discarded == 0


def test_helper_returns_empty_for_unknown_entity(engine):
    result = _get_empty_run_results_for_entity(engine, "UNKNOWN", limit=5)
    assert result == []


def test_helper_respects_limit(engine):
    """Only the N most recent runs are considered."""
    for day in range(1, 6):  # 5 runs
        run_id = uuid.uuid4()
        with Session(engine) as s:
            _make_run_log(s, "E1", run_id, _ts(day), _ts(day, 23), _ts(day, 23))
            _make_bullet_log(s, run_id, "E1", f"t{day}", is_active=False, discard_stage="grounding")

    result = _get_empty_run_results_for_entity(engine, "E1", limit=2)

    assert len(result) == 2


def test_helper_mixed_active_and_empty(engine):
    """Only runs with no active bullets are returned; runs with active bullets are skipped."""
    run_active = uuid.uuid4()
    run_empty = uuid.uuid4()
    with Session(engine) as s:
        _make_run_log(s, "E1", run_active, _ts(6), _ts(7), _ts(7, 12))
        _make_bullet_log(s, run_active, "E1", "ta", is_active=True)

        _make_run_log(s, "E1", run_empty, _ts(7), _ts(8), _ts(8, 12))
        _make_bullet_log(s, run_empty, "E1", "te", is_active=False, discard_stage="grounding", text="bad")

    result = _get_empty_run_results_for_entity(engine, "E1", limit=5)

    assert len(result) == 1
    assert str(result[0].run_id) == str(run_empty)
    assert result[0].discarded_by_grounding == ["bad"]


# ── get_bullets — merged behaviour ───────────────────────────────────────────


def test_get_bullets_shows_empty_run_not_stale_bullets(engine):
    """Core bug: Day1 has 3 active bullets; Day2 has 0 (all discarded).
    get_bullets(max_runs=1) must return Day2 with 0 bullets, not Day1's bullets.
    """
    # Day 1: run with 3 active bullets in SQLGeneratedBulletPoint
    run1 = uuid.uuid4()
    ws1, we1 = _ts(7), _ts(7, 23)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E1", kg_name="Acme Corp"))
        s.commit()
        _make_run_log(s, "E1", run1, ws1, we1, _ts(7, 23))
        _make_bullet_log(s, run1, "E1", "ta", is_active=True)
        _make_bullet_log(s, run1, "E1", "tb", is_active=True)
        _make_bullet_log(s, run1, "E1", "tc", is_active=True)
        _make_generated_bullet(s, "E1", "Acme Corp", ws1, we1, _ts(7, 23), "bullet A")
        _make_generated_bullet(s, "E1", "Acme Corp", ws1, we1, _ts(7, 23), "bullet B")
        _make_generated_bullet(s, "E1", "Acme Corp", ws1, we1, _ts(7, 23), "bullet C")

    # Day 2: run with 0 active bullets (all discarded) — nothing in SQLGeneratedBulletPoint
    run2 = uuid.uuid4()
    ws2, we2 = _ts(8), _ts(8, 23)
    with Session(engine) as s:
        _make_run_log(s, "E1", run2, ws2, we2, _ts(8, 23))
        _make_bullet_log(s, run2, "E1", "td", is_active=False, discard_stage="novelty_search", text="duplicate")
        _make_bullet_log(s, run2, "E1", "te", is_active=False, discard_stage="relevance_score", text="irrelevant")

    with patch("bigdata_briefs.api.routes.reports.get_engine", return_value=engine):
        resp = get_bullets(BatchBulletsRequest(entity_ids=["E1"], max_runs=1))

    assert resp.total_entities == 1
    entity = resp.results[0]
    assert entity.found is True
    assert len(entity.runs) == 1

    run = entity.runs[0]
    # Must show Day 2, not Day 1
    assert run.bullet_count == 0
    assert run.bullets_saved == 0
    assert run.bullets_discarded == 2
    assert run.discarded_by_novelty == ["duplicate"]
    assert run.discarded_by_relevance == ["irrelevant"]
    assert run.report_window_start == ws2.replace(tzinfo=None)
    assert run.report_window_end == we2.replace(tzinfo=None)


def test_get_bullets_max_runs_2_includes_empty_and_active(engine):
    """max_runs=2 should return [Day2 empty, Day1 with bullets]."""
    run1 = uuid.uuid4()
    ws1, we1 = _ts(7), _ts(7, 23)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E2", kg_name="Beta Inc"))
        s.commit()
        _make_run_log(s, "E2", run1, ws1, we1, _ts(7, 23))
        _make_bullet_log(s, run1, "E2", "ta", is_active=True)
        _make_generated_bullet(s, "E2", "Beta Inc", ws1, we1, _ts(7, 23))

    run2 = uuid.uuid4()
    ws2, we2 = _ts(8), _ts(8, 23)
    with Session(engine) as s:
        _make_run_log(s, "E2", run2, ws2, we2, _ts(8, 23))
        _make_bullet_log(s, run2, "E2", "tb", is_active=False, discard_stage="grounding", text="bad grounding")

    with patch("bigdata_briefs.api.routes.reports.get_engine", return_value=engine):
        resp = get_bullets(BatchBulletsRequest(entity_ids=["E2"], max_runs=2))

    entity = resp.results[0]
    assert len(entity.runs) == 2

    # Newest first: Day2 (empty), then Day1 (3 bullets)
    assert entity.runs[0].bullet_count == 0
    assert entity.runs[0].report_window_start == ws2.replace(tzinfo=None)
    assert entity.runs[1].bullet_count == 1
    assert entity.runs[1].report_window_start == ws1.replace(tzinfo=None)


def test_get_bullets_unchanged_when_latest_run_has_active_bullets(engine):
    """When the latest run has active bullets, behaviour is identical to before."""
    run1 = uuid.uuid4()
    ws1, we1 = _ts(7), _ts(7, 23)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E3", kg_name="Gamma Ltd"))
        s.commit()
        _make_run_log(s, "E3", run1, ws1, we1, _ts(7, 23))
        _make_bullet_log(s, run1, "E3", "ta", is_active=True)
        _make_generated_bullet(s, "E3", "Gamma Ltd", ws1, we1, _ts(7, 23), "active bullet text")

    with patch("bigdata_briefs.api.routes.reports.get_engine", return_value=engine):
        resp = get_bullets(BatchBulletsRequest(entity_ids=["E3"], max_runs=1))

    entity = resp.results[0]
    assert len(entity.runs) == 1
    assert entity.runs[0].bullet_count == 1
    assert entity.runs[0].bullets[0].text == "active bullet text"

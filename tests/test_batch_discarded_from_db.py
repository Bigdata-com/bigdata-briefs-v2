"""Tests that _load_discarded_for_runs and _build_entity_result_from_run_log
read discarded bullet text from SQLBulletRunLog instead of output_json."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)
from bigdata_briefs.api.routes.batch import (
    _load_discarded_for_runs,
    _build_entity_result_from_run_log,
    _stage_to_category,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


def _make_run_log(session, entity_id, window_start, window_end, run_id=None) -> SQLEntityPipelineRunLog:
    run_id = run_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    row = SQLEntityPipelineRunLog(
        run_id=run_id,
        entity_id=entity_id,
        report_window_start=window_start,
        report_window_end=window_end,
        process_started_at_utc=now,
        process_completed_at_utc=now,
        status="succeeded",
        output_json=None,  # explicitly empty — must not be read
    )
    session.add(row)
    session.commit()
    return row


def _make_bullet(session, run_id, entity_id, trace_id, is_active, discard_stage=None, text="bullet text"):
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


# ── _stage_to_category ────────────────────────────────────────────────────────


def test_stage_to_category_relevance():
    assert _stage_to_category("relevance_score") == "relevance"


def test_stage_to_category_grounding():
    assert _stage_to_category("grounding") == "grounding"


def test_stage_to_category_novelty_embedding():
    assert _stage_to_category("novelty_embedding") == "novelty"
    assert _stage_to_category("novelty_embedding_relevance") == "novelty"


def test_stage_to_category_novelty_search():
    assert _stage_to_category("novelty_search") == "novelty"
    assert _stage_to_category("novelty_search_relevance") == "novelty"


def test_stage_to_category_unknown_returns_none():
    assert _stage_to_category("unknown") is None
    assert _stage_to_category(None) is None


# ── _load_discarded_for_runs ──────────────────────────────────────────────────


def test_load_discarded_reads_from_bullet_run_log(engine):
    ws = datetime(2025, 1, 1, tzinfo=timezone.utc)
    we = datetime(2025, 1, 1, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as s:
        row = _make_run_log(s, "E1", ws, we)
        _make_bullet(s, row.run_id, "E1", "t1", False, "relevance_score", "off-topic bullet")
        _make_bullet(s, row.run_id, "E1", "t2", False, "grounding", "wrong entity")
        _make_bullet(s, row.run_id, "E1", "t3", False, "novelty_embedding", "old news")
        _make_bullet(s, row.run_id, "E1", "t4", True, None, "published bullet")  # active — must be excluded

    run_info = {"gen-run-1": ("E1", ws, we)}

    with patch("bigdata_briefs.api.routes.batch.get_engine", return_value=engine):
        result = _load_discarded_for_runs(run_info)

    assert result["gen-run-1"]["relevance"] == ["off-topic bullet"]
    assert result["gen-run-1"]["grounding"] == ["wrong entity"]
    assert result["gen-run-1"]["novelty"] == ["old news"]


def test_load_discarded_empty_when_no_run_log(engine):
    ws = datetime(2025, 2, 1, tzinfo=timezone.utc)
    we = datetime(2025, 2, 1, 23, 59, 59, tzinfo=timezone.utc)
    run_info = {"gen-run-x": ("MISSING", ws, we)}

    with patch("bigdata_briefs.api.routes.batch.get_engine", return_value=engine):
        result = _load_discarded_for_runs(run_info)

    assert result["gen-run-x"] == {"relevance": [], "grounding": [], "novelty": []}


def test_load_discarded_empty_input():
    result = _load_discarded_for_runs({})
    assert result == {}


def test_load_discarded_does_not_read_output_json(engine):
    """output_json is NULL — if the function tried to parse it, it would return nothing."""
    ws = datetime(2025, 3, 1, tzinfo=timezone.utc)
    we = datetime(2025, 3, 1, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as s:
        row = _make_run_log(s, "E2", ws, we)
        # output_json is None (set in _make_run_log); bullet data comes from SQLBulletRunLog only
        _make_bullet(s, row.run_id, "E2", "tb", False, "novelty_search", "stale news")

    run_info = {"gen-run-2": ("E2", ws, we)}

    with patch("bigdata_briefs.api.routes.batch.get_engine", return_value=engine):
        result = _load_discarded_for_runs(run_info)

    assert result["gen-run-2"]["novelty"] == ["stale news"]


def test_load_discarded_unknown_stage_not_categorised(engine):
    ws = datetime(2025, 4, 1, tzinfo=timezone.utc)
    we = datetime(2025, 4, 1, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as s:
        row = _make_run_log(s, "E3", ws, we)
        _make_bullet(s, row.run_id, "E3", "tu", False, "unknown", "mystery bullet")

    run_info = {"gen-run-3": ("E3", ws, we)}

    with patch("bigdata_briefs.api.routes.batch.get_engine", return_value=engine):
        result = _load_discarded_for_runs(run_info)

    assert result["gen-run-3"]["relevance"] == []
    assert result["gen-run-3"]["grounding"] == []
    assert result["gen-run-3"]["novelty"] == []


# ── _build_entity_result_from_run_log ─────────────────────────────────────────


def test_build_entity_result_reads_from_bullet_run_log(engine):
    ws = datetime(2025, 5, 1, tzinfo=timezone.utc)
    we = datetime(2025, 5, 1, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E4", kg_name="Acme Corp"))
        s.commit()
        row = _make_run_log(s, "E4", ws, we)
        _make_bullet(s, row.run_id, "E4", "ta", False, "relevance_score", "irrelevant")
        _make_bullet(s, row.run_id, "E4", "tb", False, "novelty_search", "duplicate")
        _make_bullet(s, row.run_id, "E4", "tc", True, None, "published")

    result = _build_entity_result_from_run_log("E4", engine)

    assert result.found is True
    assert result.entity_name == "Acme Corp"
    assert len(result.runs) == 1
    run = result.runs[0]
    assert run.discarded_by_relevance == ["irrelevant"]
    assert run.discarded_by_novelty == ["duplicate"]
    assert run.discarded_by_grounding == []
    assert run.bullets_discarded == 2


def test_build_entity_result_no_runs_returns_not_found(engine):
    result = _build_entity_result_from_run_log("MISSING", engine)
    assert result.found is False


def test_build_entity_result_does_not_read_output_json(engine):
    """output_json is NULL — proves the function reads from SQLBulletRunLog."""
    ws = datetime(2025, 6, 1, tzinfo=timezone.utc)
    we = datetime(2025, 6, 1, 23, 59, 59, tzinfo=timezone.utc)

    with Session(engine) as s:
        row = _make_run_log(s, "E5", ws, we)
        _make_bullet(s, row.run_id, "E5", "tg", False, "grounding", "wrong company")

    result = _build_entity_result_from_run_log("E5", engine)

    assert result.found is True
    assert result.runs[0].discarded_by_grounding == ["wrong company"]
    assert result.runs[0].bullets_discarded == 1

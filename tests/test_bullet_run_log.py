"""Tests for SQLBulletRunLog persistence and the helper functions in entity_runner."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.orchestration.entity_runner import _flush_bullet_run_log, _get_discard_stage
from bigdata_briefs.orchestration.models import SQLBulletRunLog
from bigdata_briefs.api.routes.ui import _bullet_stats


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


# ── _get_discard_stage ────────────────────────────────────────────────────────


def test_get_discard_stage_returns_none_for_active():
    bp = {"is_active": True}
    assert _get_discard_stage(bp) is None


def test_get_discard_stage_relevance():
    bp = {"is_active": False, "relevance_scoring": {"passed": False, "score": 1}}
    assert _get_discard_stage(bp) == "relevance_score"


def test_get_discard_stage_grounding():
    bp = {
        "is_active": False,
        "relevance_scoring": {"passed": True},
        "entity_grounding": {"check": {"decision": "invalid"}},
    }
    assert _get_discard_stage(bp) == "grounding"


def test_get_discard_stage_novelty_embedding():
    bp = {
        "is_active": False,
        "relevance_scoring": {"passed": True},
        "entity_grounding": {"check": {"decision": "valid"}},
        "novelty_embedding": {"judgment": {"decision": "discard"}},
    }
    assert _get_discard_stage(bp) == "novelty_embedding"


def test_get_discard_stage_novelty_embedding_relevance():
    bp = {
        "is_active": False,
        "relevance_scoring": {"passed": True},
        "entity_grounding": {"check": {"decision": "valid"}},
        "novelty_embedding": {
            "judgment": {"decision": "keep"},
            "relevance_check": {"passed": False},
        },
    }
    assert _get_discard_stage(bp) == "novelty_embedding_relevance"


def test_get_discard_stage_novelty_search():
    bp = {
        "is_active": False,
        "relevance_scoring": {"passed": True},
        "entity_grounding": {"check": {"decision": "valid"}},
        "novelty_embedding": {"judgment": {"decision": "keep"}},
        "novelty_search": {"search": {"verdict": "discard"}},
    }
    assert _get_discard_stage(bp) == "novelty_search"


def test_get_discard_stage_novelty_search_relevance():
    bp = {
        "is_active": False,
        "relevance_scoring": {"passed": True},
        "entity_grounding": {"check": {"decision": "valid"}},
        "novelty_embedding": {"judgment": {"decision": "keep"}},
        "novelty_search": {
            "search": {"verdict": "keep"},
            "relevance_check": {"passed": False},
        },
    }
    assert _get_discard_stage(bp) == "novelty_search_relevance"


def test_get_discard_stage_unknown_fallback():
    bp = {"is_active": False}
    assert _get_discard_stage(bp) == "unknown"


# ── _flush_bullet_run_log ─────────────────────────────────────────────────────


def _make_active_bp(trace_id: str = "t1") -> dict:
    return {
        "trace_id": trace_id,
        "is_active": True,
        "text": "Final text",
        "theme": "earnings",
        "citations": [],
        "generation": {"original_text": "Draft text"},
        "relevance_scoring": {"score": 4, "passed": True, "reason": "relevant"},
        "entity_grounding": {"check": {"decision": "valid", "reason": "ok"}},
        "novelty_embedding": {
            "judgment": {"decision": "keep", "reason": "novel"},
            "rewrite": None,
            "relevance_check": {"score": 4, "passed": True},
        },
        "novelty_search": {
            "search": {
                "verdict": "keep",
                "overall_verdict": "novel",
                "reason": "no match",
                "duration_seconds": 1.2,
            },
            "relevance_check": {"score": 4, "passed": True},
        },
    }


def _make_discarded_bp(trace_id: str = "t2", stage: str = "relevance_score") -> dict:
    bp: dict = {
        "trace_id": trace_id,
        "is_active": False,
        "text": "Discarded text",
        "theme": "",
        "citations": [],
        "generation": {"original_text": "Draft"},
    }
    if stage == "relevance_score":
        bp["relevance_scoring"] = {"score": 1, "passed": False, "reason": "off-topic"}
    elif stage == "grounding":
        bp["relevance_scoring"] = {"passed": True}
        bp["entity_grounding"] = {"check": {"decision": "invalid", "reason": "wrong entity"}}
    elif stage == "novelty_embedding":
        bp["relevance_scoring"] = {"passed": True}
        bp["entity_grounding"] = {"check": {"decision": "valid"}}
        bp["novelty_embedding"] = {"judgment": {"decision": "discard", "reason": "old news"}}
    return bp


def test_flush_writes_one_row_per_bullet(engine):
    run_id = uuid.uuid4()
    final_state = {"bullet_points": [_make_active_bp("t1"), _make_discarded_bp("t2")]}
    _flush_bullet_run_log(engine, run_id, "ENTITY1", final_state)

    with Session(engine) as s:
        rows = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_id)).all()
    assert len(rows) == 2


def test_flush_active_bullet_fields(engine):
    run_id = uuid.uuid4()
    bp = _make_active_bp("active-trace")
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": [bp]})

    with Session(engine) as s:
        row = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.trace_id == "active-trace")).first()

    assert row is not None
    assert row.is_active is True
    assert row.discard_stage is None
    assert row.not_fully_novel is False
    assert row.text == "Final text"
    assert row.original_text == "Draft text"
    assert row.theme == "earnings"
    assert row.relevance_score == 4
    assert row.relevance_passed is True
    assert row.grounding_decision == "valid"
    assert row.embedding_decision == "keep"
    assert row.search_verdict == "keep"
    assert row.search_overall_verdict == "novel"
    assert row.search_duration_seconds == pytest.approx(1.2)


def test_flush_discarded_relevance(engine):
    run_id = uuid.uuid4()
    bp = _make_discarded_bp("disc-rel", "relevance_score")
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": [bp]})

    with Session(engine) as s:
        row = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.trace_id == "disc-rel")).first()

    assert row.is_active is False
    assert row.discard_stage == "relevance_score"
    assert row.relevance_score == 1
    assert row.relevance_passed is False


def test_flush_discarded_grounding(engine):
    run_id = uuid.uuid4()
    bp = _make_discarded_bp("disc-grd", "grounding")
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": [bp]})

    with Session(engine) as s:
        row = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.trace_id == "disc-grd")).first()

    assert row.discard_stage == "grounding"
    assert row.grounding_decision == "invalid"


def test_flush_discarded_novelty_embedding(engine):
    run_id = uuid.uuid4()
    bp = _make_discarded_bp("disc-ne", "novelty_embedding")
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": [bp]})

    with Session(engine) as s:
        row = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.trace_id == "disc-ne")).first()

    assert row.discard_stage == "novelty_embedding"
    assert row.embedding_decision == "discard"


def test_flush_not_fully_novel(engine):
    run_id = uuid.uuid4()
    bp = _make_active_bp("amber")
    bp["novelty_search"]["search"]["overall_verdict"] = "mixed"
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": [bp]})

    with Session(engine) as s:
        row = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.trace_id == "amber")).first()

    assert row.not_fully_novel is True


def test_flush_empty_bullet_points_writes_nothing(engine):
    run_id = uuid.uuid4()
    _flush_bullet_run_log(engine, run_id, "E1", {"bullet_points": []})

    with Session(engine) as s:
        rows = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_id)).all()
    assert rows == []


def test_flush_missing_final_state_writes_nothing(engine):
    run_id = uuid.uuid4()
    _flush_bullet_run_log(engine, run_id, "E1", {})

    with Session(engine) as s:
        rows = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_id)).all()
    assert rows == []


def test_flush_multiple_runs_isolated(engine):
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    _flush_bullet_run_log(engine, run_a, "E1", {"bullet_points": [_make_active_bp("t-a")]})
    _flush_bullet_run_log(engine, run_b, "E1", {"bullet_points": [_make_active_bp("t-b"), _make_discarded_bp("t-b2")]})

    with Session(engine) as s:
        rows_a = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_a)).all()
        rows_b = s.exec(select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_b)).all()

    assert len(rows_a) == 1
    assert len(rows_b) == 2


# ── _bullet_stats ─────────────────────────────────────────────────────────────


def _insert_bullet(session, run_id, trace_id, is_active, discard_stage=None):
    session.add(SQLBulletRunLog(
        run_id=run_id,
        entity_id="E1",
        trace_id=trace_id,
        is_active=is_active,
        not_fully_novel=False,
        discard_stage=discard_stage,
        text="x",
        created_at=datetime.now(timezone.utc),
    ))


def test_bullet_stats_empty(engine):
    run_id = uuid.uuid4()
    with Session(engine) as s:
        stats = _bullet_stats(s, run_id)
    assert stats == {"total": 0, "active": 0, "discarded": 0, "stages": {}}


def test_bullet_stats_counts(engine):
    run_id = uuid.uuid4()
    with Session(engine) as s:
        _insert_bullet(s, run_id, "t1", True)
        _insert_bullet(s, run_id, "t2", True)
        _insert_bullet(s, run_id, "t3", False, "relevance_score")
        _insert_bullet(s, run_id, "t4", False, "novelty_embedding")
        _insert_bullet(s, run_id, "t5", False, "novelty_embedding")
        s.commit()

    with Session(engine) as s:
        stats = _bullet_stats(s, run_id)

    assert stats["total"] == 5
    assert stats["active"] == 2
    assert stats["discarded"] == 3
    assert stats["stages"] == {"relevance_score": 1, "novelty_embedding": 2}


def test_bullet_stats_only_counts_own_run(engine):
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    with Session(engine) as s:
        _insert_bullet(s, run_a, "ta1", True)
        _insert_bullet(s, run_b, "tb1", False, "grounding")
        _insert_bullet(s, run_b, "tb2", False, "grounding")
        s.commit()

    with Session(engine) as s:
        stats_a = _bullet_stats(s, run_a)
        stats_b = _bullet_stats(s, run_b)

    assert stats_a["total"] == 1
    assert stats_a["active"] == 1
    assert stats_b["total"] == 2
    assert stats_b["discarded"] == 2
    assert stats_b["stages"] == {"grounding": 2}

"""Bullet pipeline checkpoint gates and SQLite storage."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.novelty.bullet_pipeline_checkpoint import (
    BulletPipelineCheckpoint,
    grounding_validator_inputs_from_checkpoints,
    is_eligible_for_novelty_embedding,
    is_eligible_for_novelty_search,
    sync_report_lists_after_grounding,
    sync_report_lists_from_checkpoints,
)
from bigdata_briefs.novelty.pipeline_checkpoint_storage import (
    PipelineCheckpointStorage,
    checkpoint_to_sql_row,
)
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint


@pytest.fixture
def cp_engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def test_is_eligible_for_novelty_embedding_requires_grounding_and_relevance() -> None:
    base = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        entity_grounding_pass=True,
    )
    assert is_eligible_for_novelty_embedding(base) is True
    assert is_eligible_for_novelty_embedding(
        base.model_copy(update={"post_generation_entity_relevance_pass": False})
    ) is False
    assert is_eligible_for_novelty_embedding(
        base.model_copy(update={"entity_grounding_pass": False})
    ) is False


def test_is_eligible_for_novelty_search_requires_completed_embedding_keep() -> None:
    ok = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        entity_grounding_pass=True,
        novelty_embedding_completed=True,
        novelty_embedding_final_decision="KEEP",
    )
    assert is_eligible_for_novelty_search(ok) is True
    assert is_eligible_for_novelty_search(
        ok.model_copy(update={"novelty_embedding_final_decision": "DISCARD"})
    ) is False
    assert is_eligible_for_novelty_search(
        ok.model_copy(update={"novelty_embedding_completed": False})
    ) is False


def test_grounding_validator_inputs_use_post_generation_relevance_score() -> None:
    skipped = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=False,
        text_after_generation="skip",
    )
    a = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        text_after_generation="b1",
        bullet_citations=["x"],
        post_generation_relevance_score=3,
    )
    b = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        text_after_generation="b2",
        bullet_citations=[],
        post_generation_relevance_score=None,
    )
    bullets, cites, scores = grounding_validator_inputs_from_checkpoints([skipped, a, b])
    assert bullets == ["b1", "b2"]
    assert cites == [["x"], []]
    assert scores == [3, 4]


def test_sync_report_lists_keeps_order_and_skips_discard() -> None:
    a = BulletPipelineCheckpoint(
        novelty_embedding_completed=True,
        novelty_embedding_final_decision="KEEP",
        text_after_novelty_embedding_rewrite="A",
        bullet_citations=["r1"],
        post_generation_relevance_score=5,
    )
    b = BulletPipelineCheckpoint(
        novelty_embedding_completed=True,
        novelty_embedding_final_decision="DISCARD",
        text_after_novelty_embedding_rewrite="",
    )
    c = BulletPipelineCheckpoint(
        novelty_embedding_completed=True,
        novelty_embedding_final_decision="REWRITE",
        text_after_novelty_embedding_rewrite="C",
        text_after_novelty_search="C2",
        bullet_citations=[],
        post_generation_relevance_score=4,
    )
    texts, cites, scores = sync_report_lists_from_checkpoints([a, b, c])
    assert texts == ["A", "C2"]
    assert cites == [["r1"], []]
    assert scores == [5, 4]


def test_sync_report_lists_after_grounding_keeps_survivors_in_order() -> None:
    discarded = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        entity_grounding_pass=False,
        text_after_generation="gone",
    )
    kept = BulletPipelineCheckpoint(
        post_generation_entity_relevance_pass=True,
        entity_grounding_pass=True,
        text_after_entity_grounding="kept text",
        bullet_citations=["c1"],
        post_generation_relevance_score=5,
    )
    texts, cites, scores = sync_report_lists_after_grounding([discarded, kept])
    assert texts == ["kept text"]
    assert cites == [["c1"]]
    assert scores == [5]


def test_pipeline_checkpoint_storage_round_trip(cp_engine) -> None:
    tid = uuid4()
    cp = BulletPipelineCheckpoint(
        bullet_trace_id=tid,
        entity_id="ent1",
        report_date=datetime(2025, 3, 1, tzinfo=timezone.utc),
        text_after_generation="hello",
        post_generation_entity_relevance_pass=True,
        entity_grounding_pass=True,
        text_after_entity_grounding="hello",
        novelty_embedding_completed=True,
        novelty_embedding_final_decision="KEEP",
        novelty_embedding_evaluator_details=[{"evaluator_name": "x", "decision": "KEEP"}],
        bullet_citations=[":ref[LIST:[CQS:a-1]]`"],
        post_generation_relevance_score=4,
    )
    store = PipelineCheckpointStorage(cp_engine)
    store.store_all([cp], saved_at=datetime(2025, 3, 2, tzinfo=timezone.utc))
    with Session(cp_engine) as session:
        rows = session.exec(select(SQLBulletPipelineCheckpoint)).all()
    assert len(rows) == 1
    assert rows[0].bullet_trace_id == tid
    assert rows[0].entity_id == "ent1"
    assert rows[0].novelty_embedding_evaluator_details == [
        {"evaluator_name": "x", "decision": "KEEP"}
    ]


def test_checkpoint_to_sql_row_maps_fields() -> None:
    cp = BulletPipelineCheckpoint(
        entity_id="e",
        novelty_embedding_final_decision="REWRITE",
    )
    row = checkpoint_to_sql_row(cp)
    assert row.entity_id == "e"
    assert row.novelty_embedding_final_decision == "REWRITE"

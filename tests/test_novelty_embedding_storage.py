"""Tests for novelty embedding storage and retrieval (_store_embedding inserts all rows)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.novelty.models import BulletPointEmbedding
from bigdata_briefs.novelty.novelty_service import NoveltyFilteringService
from bigdata_briefs.novelty.sql_models import SQLBulletPointEmbedding
from bigdata_briefs.novelty.storage import SQLiteEmbeddingStorage


@pytest.fixture
def novelty_engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def _add_row(
    session: Session,
    *,
    entity_id: str = "e1",
    day: datetime | None = None,
    embedding: list[float] | None = None,
    status_embedding: bool | None = True,
    original_text: str = "t",
) -> None:
    day = day or datetime(2025, 1, 15)
    emb = embedding if embedding is not None else [0.1, 0.2]
    row = SQLBulletPointEmbedding(
        entity_id=entity_id,
        date=day,
        embedding=emb,
        original_text=original_text,
        status_embedding=status_embedding,
    )
    session.add(row)
    session.commit()


def test_retrieve_includes_status_embedding_true(novelty_engine) -> None:
    storage = SQLiteEmbeddingStorage(novelty_engine)
    with Session(novelty_engine) as session:
        _add_row(session, original_text="a", status_embedding=True)
    out = storage.retrieve(
        "e1", start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1)
    )
    assert len(out) == 1
    assert out[0].original_text == "a"
    assert out[0].status_embedding is True


def test_retrieve_includes_legacy_null_status_embedding(novelty_engine) -> None:
    storage = SQLiteEmbeddingStorage(novelty_engine)
    with Session(novelty_engine) as session:
        _add_row(session, original_text="legacy", status_embedding=None)
    out = storage.retrieve(
        "e1", start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1)
    )
    assert len(out) == 1
    assert out[0].original_text == "legacy"


def test_retrieve_excludes_status_embedding_false(novelty_engine) -> None:
    storage = SQLiteEmbeddingStorage(novelty_engine)
    with Session(novelty_engine) as session:
        _add_row(session, original_text="hidden", status_embedding=False)
    out = storage.retrieve(
        "e1", start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1)
    )
    assert out == []


def test_retrieve_novelty_search_archive_row(novelty_engine) -> None:
    storage = SQLiteEmbeddingStorage(novelty_engine)
    with Session(novelty_engine) as session:
        row = SQLBulletPointEmbedding(
            entity_id="e1",
            date=datetime(2025, 1, 15),
            embedding=[0.0, 1.0],
            original_text="archived",
            status="keep",
            novelty=True,
            status_embedding=True,
            added_past_evidence_from="discard",
            status_novelty_check_bigdata="old",
        )
        session.add(row)
        session.commit()
    out = storage.retrieve(
        "e1", start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1)
    )
    assert len(out) == 1
    assert out[0].added_past_evidence_from == "discard"
    assert out[0].status_novelty_check_bigdata == "old"


def test_store_sets_status_embedding_from_embedding(novelty_engine) -> None:
    storage = SQLiteEmbeddingStorage(novelty_engine)
    storage.store(
        [
            BulletPointEmbedding(
                date=datetime(2025, 1, 10),
                entity_id="e1",
                embedding=[1.0, 2.0],
                original_text="with vec",
            ),
            BulletPointEmbedding(
                date=datetime(2025, 1, 10),
                entity_id="e1",
                embedding=None,
                original_text="no vec",
                status_embedding=False,
            ),
        ]
    )
    with Session(novelty_engine) as session:
        rows = session.exec(select(SQLBulletPointEmbedding)).all()
    by_text = {r.original_text: r for r in rows}
    assert by_text["with vec"].status_embedding is True
    assert by_text["no vec"].status_embedding is False


def test_store_embedding_batches_all_rows_in_one_store_call(novelty_engine) -> None:
    stored: list[list[BulletPointEmbedding]] = []

    class _MemStorage(SQLiteEmbeddingStorage):
        def store(self, data: list[BulletPointEmbedding]) -> None:  # type: ignore[override]
            stored.append(list(data))

    mem = _MemStorage(novelty_engine)
    mock_emb = MagicMock()
    mock_emb.compute = MagicMock(return_value=[[0.1]])
    nfs = NoveltyFilteringService(mock_emb, mem)
    bp = [
        BulletPointEmbedding(
            date=datetime(2025, 6, 1),
            entity_id="x",
            embedding=[0.5],
            original_text="n1",
        ),
        BulletPointEmbedding(
            date=datetime(2025, 6, 1),
            entity_id="x",
            embedding=[0.6],
            original_text="n2",
        ),
    ]
    nfs._store_embedding("x", datetime(2025, 6, 1), bp)
    assert len(stored) == 1
    assert len(stored[0]) == 2


def test_novelty_embedding_step_empty_deferred_single_embedding_batch() -> None:
    """Deferred list is empty; no second embedding batch after LLM (no-evaluators path)."""
    mock_emb = MagicMock()
    mock_emb.compute = MagicMock(return_value=[[0.1, 0.2, 0.3]])
    storage = MagicMock()
    storage.get_min_date = MagicMock(return_value=None)
    nfs = NoveltyFilteringService(mock_emb, storage)
    day = datetime(2024, 6, 1)
    kept, _results, new_emb, deferred = nfs.novelty_embedding_step(
        texts=["hello"],
        entity_id="e1",
        entity_name="Acme",
        evaluators=[],
        start_date=day,
        end_date=day,
        current_date=day,
    )
    assert deferred == []
    assert kept == ["hello"]
    assert len(new_emb) == 1
    assert mock_emb.compute.call_count == 1

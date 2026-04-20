"""Tests for orchestrator bullet output queries."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from bigdata_briefs.models import ReportDates
from bigdata_briefs.novelty.sql_models import SQLBulletPointEmbedding
from bigdata_briefs.orchestration.output import (
    fetch_new_novelty_ok_bullets,
    fetch_previous_bullets,
)


@pytest.fixture
def out_engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def _add(
    session: Session,
    *,
    entity_id: str,
    day: datetime,
    text: str,
    novelty: bool | None,
    status: str | None,
    rw_start: datetime | None,
    rw_end: datetime | None,
    added_past_evidence_from: str | None = None,
) -> None:
    session.add(
        SQLBulletPointEmbedding(
            entity_id=entity_id,
            date=day,
            embedding=[0.1],
            original_text=text,
            novelty=novelty,
            status=status,
            report_window_start=rw_start,
            report_window_end=rw_end,
            status_embedding=True,
            added_past_evidence_from=added_past_evidence_from,
        )
    )
    session.commit()


def test_previous_and_new_split(out_engine) -> None:
    cur_start = datetime(2025, 6, 10, 0, 0, tzinfo=timezone.utc)
    cur_end = datetime(2025, 6, 11, 12, 0, tzinfo=timezone.utc)
    rd = ReportDates(start=cur_start, end=cur_end)

    with Session(out_engine) as session:
        _add(
            session,
            entity_id="e1",
            day=datetime(2025, 6, 1, tzinfo=timezone.utc),
            text="old kept",
            novelty=True,
            status="keep",
            rw_start=datetime(2025, 5, 1, tzinfo=timezone.utc),
            rw_end=datetime(2025, 5, 2, tzinfo=timezone.utc),
        )
        _add(
            session,
            entity_id="e1",
            day=datetime(2025, 6, 10, tzinfo=timezone.utc),
            text="this run keep",
            novelty=True,
            status="keep",
            rw_start=cur_start,
            rw_end=cur_end,
        )
        _add(
            session,
            entity_id="e1",
            day=datetime(2025, 6, 10, tzinfo=timezone.utc),
            text="this run discard",
            novelty=False,
            status="discard_by_novelty",
            rw_start=cur_start,
            rw_end=cur_end,
        )

    prev = fetch_previous_bullets(out_engine, "e1", rd)
    assert len(prev) == 1
    assert prev[0]["original_text"] == "old kept"

    new_ok = fetch_new_novelty_ok_bullets(out_engine, "e1", rd)
    assert len(new_ok) == 1
    assert new_ok[0]["original_text"] == "this run keep"


def test_new_novelty_ok_excludes_mixed_rewrite_mirror_row(out_engine) -> None:
    """One logical ``mixed`` verdict stores rewrite + canonical rows; output should list once."""
    cur_start = datetime(2025, 6, 10, 0, 0, tzinfo=timezone.utc)
    cur_end = datetime(2025, 6, 11, 12, 0, tzinfo=timezone.utc)
    rd = ReportDates(start=cur_start, end=cur_end)
    day = datetime(2025, 6, 10, tzinfo=timezone.utc)

    with Session(out_engine) as session:
        _add(
            session,
            entity_id="e1",
            day=day,
            text="before rewrite",
            novelty=True,
            status="keep",
            rw_start=cur_start,
            rw_end=cur_end,
            added_past_evidence_from="rewrite",
        )
        _add(
            session,
            entity_id="e1",
            day=day,
            text="after rewrite",
            novelty=True,
            status="keep",
            rw_start=cur_start,
            rw_end=cur_end,
            added_past_evidence_from="canonical",
        )

    new_ok = fetch_new_novelty_ok_bullets(out_engine, "e1", rd)
    assert len(new_ok) == 1
    assert new_ok[0]["original_text"] == "after rewrite"

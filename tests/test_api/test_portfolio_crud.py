"""Tests for portfolio add/remove endpoints (single + bulk).

Covers the unified `entity_ids` contract on both add and remove:
- single and multiple entities in one call
- duplicate add -> already_exists, missing remove -> not_found (best-effort)
- empty input -> HTTP 400
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from bigdata_briefs.api.routes import frontend
from bigdata_briefs.api.routes.frontend import (
    _PortfolioAddBody,
    _PortfolioRemoveBody,
    add_to_portfolio,
    remove_from_portfolio,
)
from bigdata_briefs.orchestration.models import SQLUserPortfolio


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with patch.object(frontend, "get_engine", return_value=eng):
        yield eng


def _ids(eng):
    with Session(eng) as s:
        return {r.entity_id for r in s.exec(select(SQLUserPortfolio)).all()}


def test_add_single(engine):
    resp = add_to_portfolio(_PortfolioAddBody(entity_id="0157B1"))
    assert resp["added"] == 1
    assert resp["results"] == [
        {"entity_id": "0157B1", "status": "added", "entity_name": "0157B1", "kg_ticker": None}
    ]
    assert _ids(engine) == {"0157B1"}


def test_add_bulk_with_duplicate(engine):
    add_to_portfolio(_PortfolioAddBody(entity_ids=["0157B1"]))
    resp = add_to_portfolio(_PortfolioAddBody(entity_ids=["0157B1", "D8442A", "228D42"]))
    assert resp["added"] == 2
    statuses = {r["entity_id"]: r["status"] for r in resp["results"]}
    assert statuses == {"0157B1": "already_exists", "D8442A": "added", "228D42": "added"}
    assert _ids(engine) == {"0157B1", "D8442A", "228D42"}


def test_add_dedupes_and_trims(engine):
    resp = add_to_portfolio(_PortfolioAddBody(entity_ids=[" 0157B1 ", "0157B1", ""]))
    assert resp["added"] == 1
    assert _ids(engine) == {"0157B1"}


def test_add_empty_raises_400(engine):
    with pytest.raises(HTTPException) as exc:
        add_to_portfolio(_PortfolioAddBody(entity_ids=[]))
    assert exc.value.status_code == 400


def test_remove_bulk_with_missing(engine):
    add_to_portfolio(_PortfolioAddBody(entity_ids=["0157B1", "D8442A"]))
    resp = remove_from_portfolio(_PortfolioRemoveBody(entity_ids=["0157B1", "ZZZZZZ"]))
    assert resp["removed"] == 1
    statuses = {r["entity_id"]: r["status"] for r in resp["results"]}
    assert statuses == {"0157B1": "removed", "ZZZZZZ": "not_found"}
    assert _ids(engine) == {"D8442A"}


def test_remove_single_legacy_field(engine):
    add_to_portfolio(_PortfolioAddBody(entity_ids=["0157B1"]))
    resp = remove_from_portfolio(_PortfolioRemoveBody(entity_id="0157B1"))
    assert resp["removed"] == 1
    assert _ids(engine) == set()


def test_remove_empty_raises_400(engine):
    with pytest.raises(HTTPException) as exc:
        remove_from_portfolio(_PortfolioRemoveBody(entity_ids=[]))
    assert exc.value.status_code == 400

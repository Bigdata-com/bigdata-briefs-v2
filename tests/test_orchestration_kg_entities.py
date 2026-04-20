"""Unit tests for KG ticker helper and entity mapping."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bigdata_briefs.models import Entity
from bigdata_briefs.orchestration.kg_entities import (
    fetch_kg_entities_by_ids,
    kg_record_to_entity_dict,
    ticker_from_listing_values,
)


def test_ticker_first_listing_after_colon() -> None:
    assert ticker_from_listing_values(["XNAS:AAPL", "XBKK:AAPL80"]) == "AAPL"
    assert ticker_from_listing_values(["XBKK:AAPL80"]) == "AAPL80"
    assert ticker_from_listing_values([]) is None


def test_entity_from_kg_record() -> None:
    rec = {
        "id": "D8442A",
        "name": "Apple Inc.",
        "category": "companies",
        "listing_values": ["XNAS:AAPL"],
    }
    d = kg_record_to_entity_dict(rec)
    ent = Entity.from_api(d)
    assert ent.id == "D8442A"
    assert ent.name == "Apple Inc."
    assert ent.entity_type == "companies"
    assert ent.ticker == "AAPL"


def test_kg_record_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        kg_record_to_entity_dict(
            {"id": "x", "name": "   ", "category": "companies"},
        )


def test_kg_record_rejects_missing_category() -> None:
    with pytest.raises(ValueError, match="category"):
        kg_record_to_entity_dict({"id": "x", "name": "Co", "category": ""})


def test_kg_record_rejects_bad_listing_values_type() -> None:
    with pytest.raises(ValueError, match="listing_values"):
        kg_record_to_entity_dict(
            {"id": "x", "name": "Co", "category": "c", "listing_values": "X:Y"},
        )


class _FakeKgResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeKgResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_fetch_kg_raises_when_response_omits_requested_id() -> None:
    payload = b'{"results": {}}'
    with patch(
        "bigdata_briefs.orchestration.kg_entities.urlopen",
        return_value=_FakeKgResponse(payload),
    ):
        with pytest.raises(ValueError, match="missing"):
            fetch_kg_entities_by_ids(["missing"], api_key="k", base_url="https://api.example.test")

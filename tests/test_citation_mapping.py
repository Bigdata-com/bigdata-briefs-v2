"""Tests for API citation dict mapping (read path from stored JSON)."""

from __future__ import annotations

from bigdata_briefs.api.citation_mapping import stored_citation_dict_to_detail


def test_stored_citation_dict_to_detail_full() -> None:
    d = stored_citation_dict_to_detail(
        {
            "id": "CQS:ABC-1",
            "headline": "H",
            "text": "T",
            "source_name": "Benzinga",
        }
    )
    assert d.id == "CQS:ABC-1"
    assert d.headline == "H"
    assert d.text == "T"
    assert d.source_name == "Benzinga"
    out = d.model_dump()
    assert out["source_name"] == "Benzinga"


def test_stored_citation_dict_to_detail_missing_source_name() -> None:
    d = stored_citation_dict_to_detail(
        {"id": "CQS:X-1", "headline": "h", "text": "t"},
    )
    assert d.source_name == ""


def test_stored_citation_dict_to_detail_coerces_types() -> None:
    d = stored_citation_dict_to_detail({})
    assert d.id == ""
    assert d.headline == ""
    assert d.text == ""
    assert d.source_name == ""

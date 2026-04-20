"""Tests: entity must come from KG precache, orchestration SQLite cache, or live KG."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bigdata_briefs.orchestration.entity_runner import EntityResolutionError, resolve_entity_for_run


def test_resolve_kg_error_without_sqlite_cache_raises() -> None:
    orch = SimpleNamespace(kg_payload_json=None)
    with patch(
        "bigdata_briefs.orchestration.entity_runner.fetch_kg_entities_by_ids",
        side_effect=RuntimeError("network"),
    ):
        with pytest.raises(EntityResolutionError, match="Knowledge Graph request failed"):
            resolve_entity_for_run(
                entity_id="x",
                orch=orch,
                refresh_entity=True,
                kg_precache=None,
            )


def test_resolve_empty_kg_without_cache_raises() -> None:
    orch = SimpleNamespace(kg_payload_json=None)
    with patch(
        "bigdata_briefs.orchestration.entity_runner.fetch_kg_entities_by_ids",
        side_effect=ValueError(
            "Knowledge Graph response missing or invalid (non-object) results for entity_ids=['missing']"
        ),
    ):
        with pytest.raises(EntityResolutionError, match="Knowledge Graph request failed"):
            resolve_entity_for_run(
                entity_id="missing",
                orch=orch,
                refresh_entity=True,
                kg_precache=None,
            )


def test_resolve_invalid_sqlite_json_raises() -> None:
    orch = SimpleNamespace(kg_payload_json="{not json")
    with patch("bigdata_briefs.orchestration.entity_runner.fetch_kg_entities_by_ids") as fetch_mock:
        with pytest.raises(EntityResolutionError, match="invalid JSON"):
            resolve_entity_for_run(
                entity_id="e1",
                orch=orch,
                refresh_entity=False,
                kg_precache=None,
            )
        fetch_mock.assert_not_called()


def test_resolve_invalid_kg_shape_in_cache_raises() -> None:
    orch = SimpleNamespace(kg_payload_json='"scalar"')
    with patch("bigdata_briefs.orchestration.entity_runner.fetch_kg_entities_by_ids") as fetch_mock:
        with pytest.raises(EntityResolutionError, match="must decode to an object"):
            resolve_entity_for_run(
                entity_id="e1",
                orch=orch,
                refresh_entity=False,
                kg_precache=None,
            )
        fetch_mock.assert_not_called()


def test_resolve_uses_sqlite_kg_payload_without_live_call() -> None:
    rec = {
        "id": "e1",
        "name": "Foo Co",
        "category": "company",
        "listing_values": ["XNAS:FOO"],
    }
    orch = SimpleNamespace(kg_payload_json=json.dumps(rec))
    with patch(
        "bigdata_briefs.orchestration.entity_runner.fetch_kg_entities_by_ids",
    ) as fetch_mock:
        ent, kg = resolve_entity_for_run(
            entity_id="e1",
            orch=orch,
            refresh_entity=False,
            kg_precache=None,
        )
        fetch_mock.assert_not_called()
    assert kg is None
    assert ent.id == "e1"
    assert ent.name == "Foo Co"
    assert ent.ticker == "FOO"

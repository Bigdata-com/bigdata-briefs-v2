"""Tests for the database-less stateless run path.

These exercise the no-network building blocks and assert the dependency container
is engine-free (no SQLite). The full graph invoke is covered by integration tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bigdata_briefs.graph.dependencies import RuntimeDependencies
from bigdata_briefs.graph.graph import compile_brief_graph
from bigdata_briefs.graph.state import make_empty_state_defaults
from bigdata_briefs.orchestration.stateless_runner import (
    _build_entity_report,
    build_stateless_dependencies,
    phase_for_node,
    resolve_entity_stateless,
    run_entity_stateless,
)


def _entity(eid: str = "E1", name: str = "Test Corp"):
    return SimpleNamespace(id=eid, name=name)


def test_build_entity_report_groups_bullets_citations_and_discards():
    final_state = {
        "bullet_points": [
            {
                "is_active": True,
                "text": "Apple raised guidance.",
                "citations": ["CQS:DOC1-3"],
                "novelty_search": {"search": {"verdict": "keep", "overall_verdict": "novel"}},
            },
            {"is_active": False, "relevance_scoring": {"passed": False}, "text": "low relevance"},
            {"is_active": False, "novelty_search": {"search": {"verdict": "discard"}}, "text": "not new"},
        ],
        "source_references": {
            "ref0": {
                "document_id": "DOC1",
                "chunk_id": 3,
                "headline": "Apple lifts outlook",
                "source_name": "Reuters",
                "url": "https://ex.com/a",
            }
        },
    }
    rep = _build_entity_report(_entity(name="Apple Inc."), final_state)

    assert rep["entity_name"] == "Apple Inc."
    assert rep["bullets_saved"] == 1
    assert rep["bullets_discarded"] == 2

    bullet = rep["bullets"][0]
    assert bullet["text"] == "Apple raised guidance."
    assert bullet["search_action"] == "keep"
    assert bullet["is_novel"] is True  # overall_verdict "novel" => fully novel
    assert "not_fully_novel" not in bullet
    # citation has only source_name/headline/url — no CQS id surfaced
    assert bullet["citations"][0] == {
        "source_name": "Reuters",
        "headline": "Apple lifts outlook",
        "url": "https://ex.com/a",
    }
    assert "id" not in bullet["citations"][0]

    assert rep["discarded_by_relevance"] == ["low relevance"]
    assert rep["discarded_by_novelty"] == ["not new"]


def test_build_entity_report_unresolved_citation_is_empty():
    final_state = {
        "bullet_points": [
            {"is_active": True, "text": "x", "citations": ["CQS:MISSING-9"]}
        ],
        "source_references": {},
    }
    rep = _build_entity_report(_entity(), final_state)
    assert rep["bullets"][0]["citations"][0] == {
        "source_name": "",
        "headline": "",
        "url": None,
    }


def _no_engine_deps():
    deps = RuntimeDependencies(
        engine=None,
        query_service=MagicMock(),
        llm_client=MagicMock(),
        brief_service=MagicMock(),
        novelty_service=MagicMock(),
        embedding_client=None,
        embedding_storage=None,
        generated_bullet_storage=None,
    )
    deps.query_service.run_exploratory_search.return_value = []
    return deps


_BASE_STATE = {
    "entity_id": "E1",
    "entity_name": "Test Corp",
    "entity_type": "company",
    "entity_ticker": "TC",
    "report_start_date": "2026-01-01",
    "report_end_date": "2026-01-02",
    "request_id": "r1",
    "config": {},
}


def test_resolve_entity_from_metadata_does_no_network():
    e = resolve_entity_stateless(
        entity_id="ABC123",
        entity_metadata={"name": "Apple Inc.", "category": "Companies", "ticker": "AAPL"},
    )
    assert e.id == "ABC123"
    assert e.name == "Apple Inc."
    assert e.ticker == "AAPL"


def test_run_rejects_inverted_window():
    with pytest.raises(ValueError):
        run_entity_stateless(
            entity_id="X",
            window_start=datetime(2026, 1, 2, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
            pipeline_config={},
        )


def test_stateless_dependencies_have_no_engine_or_storage():
    deps = build_stateless_dependencies()
    assert deps.engine is None
    assert deps.embedding_storage is None
    assert deps.generated_bullet_storage is None
    assert deps.query_service.chunk_filter_service is None
    # brief_service is still needed by Phase-1 extract_concepts
    assert deps.brief_service is not None
    assert deps.llm_client is not None


def test_stateless_graph_runs_with_no_engine():
    """The stateless graph must execute end-to-end with engine=None (no DB access).

    With no exploratory chunks the graph exits early at no_data. Crucially it never
    runs initialize_pipeline (schema creation) and never dereferences deps.engine.
    """
    deps = _no_engine_deps()
    state = {**make_empty_state_defaults(), **_BASE_STATE}
    graph = compile_brief_graph(stateless=True)
    with patch(
        "bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search.settings"
    ) as ms:
        ms.API_SIMULTANEOUS_REQUESTS = 4
        final = graph.invoke(state, {"configurable": {"deps": deps}})

    assert final.get("pipeline_status") == "no_data"
    assert deps.engine is None


def test_stream_emits_phase_progress():
    """Driving the stateless graph via stream surfaces coarse phase labels."""
    deps = _no_engine_deps()
    state = {**make_empty_state_defaults(), **_BASE_STATE}
    graph = compile_brief_graph(stateless=True)
    phases: list[str] = []
    with patch(
        "bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search.settings"
    ) as ms:
        ms.API_SIMULTANEOUS_REQUESTS = 4
        for update in graph.stream(
            state, {"configurable": {"deps": deps}}, stream_mode="updates"
        ):
            for node_name in update:
                phases.append(phase_for_node(node_name))
    assert "search" in phases


def test_phase_for_node_maps_known_and_unknown():
    assert phase_for_node("entity_grounding_check") == "grounding"
    assert phase_for_node("novelty_search_fetch") == "novelty"
    assert phase_for_node("build_report") == "finalizing"
    assert phase_for_node("some_future_node") == "some_future_node"

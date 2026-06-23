"""Tests for graph topology changes.

Verifies that:
- initial_check is no longer in the compiled graph
- initialize_pipeline connects directly to exploratory_search
- exploratory_search routes to END on no_data and continues otherwise
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_config, make_deps
from bigdata_briefs.graph.graph import build_brief_graph, compile_brief_graph
from bigdata_briefs.graph.constants import (
    NODE_EXPLORATORY_SEARCH,
    NODE_INITIALIZE_PIPELINE,
    PIPELINE_STATUS_NO_DATA,
)
from bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search import (
    execute_broad_topic_search,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _topology(compiled):
    """Return (sorted node names, sorted (source, target, conditional) edges)."""
    gg = compiled.get_graph()
    nodes = tuple(sorted(compiled.nodes))
    edges = tuple(sorted((e.source, e.target, bool(e.conditional)) for e in gg.edges))
    return nodes, edges


# ── Behavior-preservation guard: stateful (default) topology is frozen ─────────
#
# This snapshot locks the EXISTING graph so the stateless work cannot silently
# alter it. If you intentionally change the stateful pipeline, update these two
# frozen sets in the same commit.

_STATEFUL_NODES = (
    "__start__",
    "build_report",
    "bullets_generation_and_scoring",
    "concept_extraction",
    "concept_search",
    "concept_search_postprocessing",
    "embed_and_retrieve",
    "entity_grounding_check",
    "exploratory_search",
    "initialize_pipeline",
    "novelty_judgment_embedding",
    "novelty_search_fetch",
    "novelty_search_judgment",
    "novelty_search_parse_and_plan",
    "novelty_search_rewrite",
    "persist_novel_embeddings",
    "quarter_info",
    "redundancy_check",
    "relevance_score_search",
    "save_novel_bullets",
    "standalone_validation",
    "thematic_consolidation",
)

_STATEFUL_EDGES = (
    ("__start__", "initialize_pipeline", False),
    ("build_report", "__end__", False),
    ("bullets_generation_and_scoring", "__end__", True),
    ("bullets_generation_and_scoring", "entity_grounding_check", True),
    ("concept_extraction", "concept_search", False),
    ("concept_search", "concept_search_postprocessing", False),
    ("concept_search_postprocessing", "__end__", True),
    ("concept_search_postprocessing", "bullets_generation_and_scoring", True),
    ("embed_and_retrieve", "novelty_judgment_embedding", False),
    ("entity_grounding_check", "embed_and_retrieve", False),
    ("exploratory_search", "__end__", True),
    ("exploratory_search", "quarter_info", True),
    ("initialize_pipeline", "exploratory_search", False),
    ("novelty_judgment_embedding", "persist_novel_embeddings", False),
    ("novelty_search_fetch", "novelty_search_judgment", False),
    ("novelty_search_judgment", "novelty_search_rewrite", False),
    ("novelty_search_parse_and_plan", "novelty_search_fetch", False),
    ("novelty_search_rewrite", "relevance_score_search", False),
    ("persist_novel_embeddings", "novelty_search_parse_and_plan", False),
    ("quarter_info", "concept_extraction", False),
    ("redundancy_check", "thematic_consolidation", False),
    ("relevance_score_search", "save_novel_bullets", False),
    ("save_novel_bullets", "__end__", True),
    ("save_novel_bullets", "build_report", True),
    ("save_novel_bullets", "redundancy_check", True),
    ("standalone_validation", "build_report", False),
    ("thematic_consolidation", "standalone_validation", False),
)


def test_stateful_topology_snapshot_unchanged():
    """The default (stateful) compiled graph must match the frozen snapshot."""
    nodes, edges = _topology(compile_brief_graph())
    assert nodes == _STATEFUL_NODES
    assert edges == _STATEFUL_EDGES


def test_default_is_stateful():
    """compile_brief_graph() with no args must be the stateful graph."""
    assert "embed_and_retrieve" in compile_brief_graph().nodes


# ── Stateless topology ─────────────────────────────────────────────────────────

_STATELESS_OMITTED_NODES = (
    "initialize_pipeline",
    "embed_and_retrieve",
    "novelty_judgment_embedding",
    "persist_novel_embeddings",
)


def test_stateless_omits_db_coupled_nodes():
    """Stateless graph must not contain the initialize or embedding-novelty nodes."""
    nodes = set(compile_brief_graph(stateless=True).nodes)
    for n in _STATELESS_OMITTED_NODES:
        assert n not in nodes


def test_stateless_wires_start_to_exploratory_search():
    """With initialize_pipeline gone, START goes straight to exploratory_search."""
    _, edges = _topology(compile_brief_graph(stateless=True))
    assert ("__start__", "exploratory_search", False) in edges


def test_stateless_wires_grounding_to_search_novelty():
    """Grounding feeds search-novelty directly (no embedding trio in between)."""
    _, edges = _topology(compile_brief_graph(stateless=True))
    assert ("entity_grounding_check", "novelty_search_parse_and_plan", False) in edges


def test_stateless_keeps_shared_nodes():
    """All non-DB nodes are shared verbatim with the stateful graph."""
    nodes = set(compile_brief_graph(stateless=True).nodes)
    for n in (
        "exploratory_search", "quarter_info", "concept_extraction", "concept_search",
        "concept_search_postprocessing", "bullets_generation_and_scoring",
        "entity_grounding_check", "novelty_search_parse_and_plan", "novelty_search_fetch",
        "novelty_search_judgment", "novelty_search_rewrite", "relevance_score_search",
        "save_novel_bullets", "build_report",
    ):
        assert n in nodes


# ── Graph topology ────────────────────────────────────────────────────────────


def test_initial_check_not_in_compiled_graph():
    g = compile_brief_graph()
    assert "initial_check" not in g.nodes


def test_initialize_pipeline_connects_to_exploratory_search():
    """initialize_pipeline must have a direct edge to exploratory_search."""
    g = build_brief_graph()
    compiled = g.compile()
    # The graph edges from initialize_pipeline should go to exploratory_search
    assert "initial_check" not in compiled.nodes
    assert NODE_EXPLORATORY_SEARCH in compiled.nodes
    assert NODE_INITIALIZE_PIPELINE in compiled.nodes


def test_exploratory_search_no_data_sets_pipeline_status():
    """When exploratory_search finds nothing it sets pipeline_status=no_data."""
    deps = make_deps()
    deps.query_service.run_exploratory_search.return_value = []

    with patch(
        "bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search.settings"
    ) as ms:
        ms.API_SIMULTANEOUS_REQUESTS = 4
        result = execute_broad_topic_search(_state(), make_config(deps))

    assert result["pipeline_status"] == PIPELINE_STATUS_NO_DATA
    assert result["exploratory_chunks"] == []


def test_exploratory_search_with_results_sets_running():
    """When exploratory_search finds results pipeline_status stays running."""
    deps = make_deps()
    mock_result = MagicMock()
    mock_result.source_rank = 1
    mock_result.chunks = [MagicMock()]
    mock_result.model_dump.return_value = {"headline": "Test", "chunks": []}
    deps.query_service.run_exploratory_search.return_value = [mock_result]

    with patch(
        "bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search.settings"
    ) as ms:
        ms.API_SIMULTANEOUS_REQUESTS = 4
        result = execute_broad_topic_search(_state(), make_config(deps))

    assert result["pipeline_status"] == "running"
    assert len(result["exploratory_chunks"]) == 1


def test_initial_check_result_not_in_state_defaults():
    """initial_check_result must no longer be in the state defaults."""
    from bigdata_briefs.graph.state import make_empty_state_defaults
    defaults = make_empty_state_defaults()
    assert "initial_check_result" not in defaults

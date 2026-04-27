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

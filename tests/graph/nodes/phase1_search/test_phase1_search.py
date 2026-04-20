"""Tests for all Phase 1 search nodes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_CONCEPT_EXTRACTION,
    NODE_CONCEPT_SEARCH,
    NODE_CONCEPT_SEARCH_POSTPROCESSING,
    NODE_EXPLORATORY_SEARCH,
    NODE_INITIAL_CHECK,
    NODE_QUARTER_INFO,
    PIPELINE_STATUS_NO_DATA,
)
from bigdata_briefs.graph.nodes.phase1_search.check_entity_data import (
    verify_entity_has_search_results,
)
from bigdata_briefs.graph.nodes.phase1_search.deduplicate_and_filter import (
    deduplicate_and_filter_concept_results,
)
from bigdata_briefs.graph.nodes.phase1_search.extract_concepts import (
    extract_thematic_concepts_from_chunks,
)
from bigdata_briefs.graph.nodes.phase1_search.fetch_quarter_info import (
    resolve_fiscal_quarter_from_calendar,
)
from bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search import (
    execute_broad_topic_search,
)
from bigdata_briefs.graph.nodes.phase1_search.search_by_concepts import (
    execute_parallel_concept_queries,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _fake_result(chunks=1):
    """Build a minimal mock Result object."""
    result = MagicMock()
    result.model_dump.return_value = {"headline": "Test", "chunks": []}
    result.chunks = [MagicMock() for _ in range(chunks)]
    return result


# ══════════════════════════════════════════════════════════════════════════════
# verify_entity_has_search_results (initial_check)
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifyEntityHasSearchResults:
    def test_has_results_sets_status_running(self):
        deps = make_deps()
        deps.query_service.check_if_entity_has_results.return_value = [MagicMock()]
        result = verify_entity_has_search_results(_state(), make_config(deps))

        assert result["pipeline_status"] == "running"
        assert result["initial_check_result"]["has_results"] is True
        assert result["initial_check_result"]["result_count"] == 1

    def test_no_results_sets_status_no_data(self):
        deps = make_deps()
        deps.query_service.check_if_entity_has_results.return_value = []
        result = verify_entity_has_search_results(_state(), make_config(deps))

        assert result["pipeline_status"] == PIPELINE_STATUS_NO_DATA
        assert result["initial_check_result"]["has_results"] is False

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.query_service.check_if_entity_has_results.return_value = [MagicMock()]
        result = verify_entity_has_search_results(_state(), make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["node_id"] == NODE_INITIAL_CHECK
        assert metrics["search_calls"] == 1

    def test_passes_source_filter_and_categories_from_config(self):
        deps = make_deps()
        deps.query_service.check_if_entity_has_results.return_value = []
        state = _state(config={"source_filter": "sf", "categories": ["cat1"]})
        verify_entity_has_search_results(state, make_config(deps))

        call_kwargs = deps.query_service.check_if_entity_has_results.call_args.kwargs
        assert call_kwargs["source_filter"] == "sf"
        assert call_kwargs["categories"] == ["cat1"]

    def test_service_exception_propagates(self):
        deps = make_deps()
        deps.query_service.check_if_entity_has_results.side_effect = RuntimeError("API down")
        with pytest.raises(RuntimeError, match="API down"):
            verify_entity_has_search_results(_state(), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# execute_broad_topic_search (exploratory_search)
# ══════════════════════════════════════════════════════════════════════════════

class TestExecuteBroadTopicSearch:
    def test_results_returned_populates_chunks(self):
        deps = make_deps()
        mock_result = _fake_result(chunks=3)
        deps.query_service.run_exploratory_search.return_value = [mock_result]
        result = execute_broad_topic_search(_state(), make_config(deps))

        assert result["pipeline_status"] == "running"
        assert len(result["exploratory_chunks"]) == 1

    def test_no_results_sets_no_data(self):
        deps = make_deps()
        deps.query_service.run_exploratory_search.return_value = []
        result = execute_broad_topic_search(_state(), make_config(deps))

        assert result["pipeline_status"] == PIPELINE_STATUS_NO_DATA
        assert result["exploratory_chunks"] == []

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.query_service.run_exploratory_search.return_value = []
        result = execute_broad_topic_search(_state(), make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_EXPLORATORY_SEARCH

    def test_topics_from_config_used(self):
        deps = make_deps()
        deps.query_service.run_exploratory_search.return_value = []
        state = _state(config={"topics": ["earnings", "guidance"]})
        execute_broad_topic_search(state, make_config(deps))

        call_kwargs = deps.query_service.run_exploratory_search.call_args.kwargs
        assert call_kwargs["topics"] == ["earnings", "guidance"]

    def test_defaults_to_entity_name_as_topic(self):
        deps = make_deps()
        deps.query_service.run_exploratory_search.return_value = []
        execute_broad_topic_search(_state(config={}), make_config(deps))

        call_kwargs = deps.query_service.run_exploratory_search.call_args.kwargs
        assert call_kwargs["topics"] == ["Test Corp"]

    def test_service_exception_propagates(self):
        deps = make_deps()
        deps.query_service.run_exploratory_search.side_effect = RuntimeError("timeout")
        with pytest.raises(RuntimeError, match="timeout"):
            execute_broad_topic_search(_state(), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# resolve_fiscal_quarter_from_calendar (quarter_info)
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveFiscalQuarterFromCalendar:
    def test_returns_quarter_title_from_api(self):
        with patch(
            "bigdata_briefs.graph.nodes.phase1_search.fetch_quarter_info.get_current_quarter_title",
            return_value={"ENTITY123": "Q1 2025"},
        ):
            result = resolve_fiscal_quarter_from_calendar(_state(), make_config())

        assert result["current_quarter_title"] == "Q1 2025"

    def test_returns_empty_string_when_entity_not_in_response(self):
        with patch(
            "bigdata_briefs.graph.nodes.phase1_search.fetch_quarter_info.get_current_quarter_title",
            return_value={},
        ):
            result = resolve_fiscal_quarter_from_calendar(_state(), make_config())

        assert result["current_quarter_title"] == ""

    def test_returns_node_metrics(self):
        with patch(
            "bigdata_briefs.graph.nodes.phase1_search.fetch_quarter_info.get_current_quarter_title",
            return_value={},
        ):
            result = resolve_fiscal_quarter_from_calendar(_state(), make_config())

        assert result["node_metrics"][0]["node_id"] == NODE_QUARTER_INFO


# ══════════════════════════════════════════════════════════════════════════════
# extract_thematic_concepts_from_chunks (concept_extraction)
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractThematicConceptsFromChunks:
    def _make_concepts(self, themes: list[str]):
        mock_concepts = MagicMock()
        mock_concepts.categories = [
            MagicMock(theme=t, concepts=["c1", "c2"]) for t in themes
        ]
        mock_concepts.model_dump.return_value = {"categories": []}
        return mock_concepts

    def test_themes_list_extracted_from_categories(self):
        deps = make_deps()
        concepts = self._make_concepts(["Revenue", "Margins"])
        deps.brief_service.extract_concepts.return_value = concepts
        state = _state(exploratory_chunks=[])

        result = extract_thematic_concepts_from_chunks(state, make_config(deps))

        assert result["themes"] == ["Revenue", "Margins"]

    def test_extracted_concepts_serialised(self):
        deps = make_deps()
        concepts = self._make_concepts(["Revenue"])
        deps.brief_service.extract_concepts.return_value = concepts
        result = extract_thematic_concepts_from_chunks(_state(), make_config(deps))

        assert "extracted_concepts" in result

    def test_returns_node_metrics(self):
        deps = make_deps()
        concepts = self._make_concepts([])
        deps.brief_service.extract_concepts.return_value = concepts
        result = extract_thematic_concepts_from_chunks(_state(), make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["node_id"] == NODE_CONCEPT_EXTRACTION
        assert metrics["llm_calls"] == 1

    def test_service_exception_propagates(self):
        deps = make_deps()
        deps.brief_service.extract_concepts.side_effect = RuntimeError("LLM failed")
        with pytest.raises(RuntimeError, match="LLM failed"):
            extract_thematic_concepts_from_chunks(_state(), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# execute_parallel_concept_queries (concept_search)
# ══════════════════════════════════════════════════════════════════════════════

class TestExecuteParallelConceptQueries:
    def _state_with_concepts(self):
        concepts = {
            "categories": [
                {"theme": "Revenue", "concepts": ["earnings", "revenue"]},
            ]
        }
        return _state(extracted_concepts=concepts)

    def test_returns_raw_concept_results(self):
        deps = make_deps()
        fake_result = _fake_result(chunks=2)
        deps.query_service.run_concept_queries_raw.return_value = (
            [fake_result],
            {"earnings": {"theme": "Revenue", "results": [fake_result]}},
            {"Revenue": [fake_result]},
        )
        result = execute_parallel_concept_queries(
            self._state_with_concepts(), make_config(deps)
        )

        assert "raw_concept_results" in result
        assert "all_results" in result["raw_concept_results"]
        assert "results_by_theme" in result["raw_concept_results"]

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.query_service.run_concept_queries_raw.return_value = ([], {}, {})
        result = execute_parallel_concept_queries(
            self._state_with_concepts(), make_config(deps)
        )
        assert result["node_metrics"][0]["node_id"] == NODE_CONCEPT_SEARCH

    def test_service_exception_propagates(self):
        deps = make_deps()
        deps.query_service.run_concept_queries_raw.side_effect = RuntimeError("network")
        with pytest.raises(RuntimeError, match="network"):
            execute_parallel_concept_queries(self._state_with_concepts(), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# deduplicate_and_filter_concept_results (concept_search_postprocessing)
# ══════════════════════════════════════════════════════════════════════════════

class TestDeduplicateAndFilterConceptResults:
    def _state_with_raw(self):
        return _state(
            raw_concept_results={
                "all_results": [],
                "results_per_concept": {},
                "results_by_theme": {},
                "concepts": {"categories": []},
            }
        )

    def _make_result_with_chunks(self, n_chunks=2):
        chunk = MagicMock()
        chunk.chunk_num = 0
        chunk.text = "chunk text"
        chunk.highlights = []
        r = MagicMock()
        r.document_id = "doc1"
        r.headline = "Headline"
        r.timestamp = "2025-01-01"
        r.source_name = "Reuters"
        r.source_rank = 1
        r.url = "http://example.com"
        r.chunks = [chunk] * n_chunks
        r.model_dump.return_value = {}
        return r

    def test_source_references_keyed_as_cqs_ref(self):
        deps = make_deps()
        result = self._make_result_with_chunks(n_chunks=2)
        deps.query_service.process_concept_results.return_value = (
            [result],
            {"Revenue": [result]},
        )
        out = deduplicate_and_filter_concept_results(self._state_with_raw(), make_config(deps))

        keys = list(out["source_references"].keys())
        assert all(k.startswith("CQS:REF") for k in keys)
        assert len(keys) == 2  # 2 chunks → 2 refs

    def test_processed_concept_results_structure(self):
        deps = make_deps()
        deps.query_service.process_concept_results.return_value = ([], {})
        out = deduplicate_and_filter_concept_results(self._state_with_raw(), make_config(deps))

        pcr = out["processed_concept_results"]
        assert "results" in pcr
        assert "results_by_theme" in pcr

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.query_service.process_concept_results.return_value = ([], {})
        out = deduplicate_and_filter_concept_results(self._state_with_raw(), make_config(deps))
        assert out["node_metrics"][0]["node_id"] == NODE_CONCEPT_SEARCH_POSTPROCESSING

    def test_service_exception_propagates(self):
        deps = make_deps()
        deps.query_service.process_concept_results.side_effect = RuntimeError("db error")
        with pytest.raises(RuntimeError, match="db error"):
            deduplicate_and_filter_concept_results(self._state_with_raw(), make_config(deps))

"""
Tests for the four novelty-via-search LangGraph nodes and _search_impl utilities.

Nodes tested:
  - parse_and_plan_search   (novelty_search_parse_and_plan)
  - fetch_search_evidence   (novelty_search_fetch)
  - judge_novelty_by_search (novelty_search_judgment)
  - rewrite_search_bullets  (novelty_search_rewrite)

_search_impl pure-function tests:
  - _ns_timestamp_to_date
  - _ns_compute_overall_verdict
  - _ns_validate_parse_and_plan_response
  - _ns_assign_simple_ids
  - _ns_reference_date_to_search_end
  - _ns_format_evidence_grouped_by_date_and_doc
  - _ns_get_evidence_for_claim
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_NOVELTY_SEARCH_FETCH,
    NODE_NOVELTY_SEARCH_JUDGMENT,
    NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
    NODE_NOVELTY_SEARCH_REWRITE,
)
from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NSClaim,
    _NSClaimVerdict,
    _NSParseAndPlanResponse,
    _NSSearchResult,
    _NSSentencePart,
    _ns_assign_simple_ids,
    _ns_compute_overall_verdict,
    _ns_format_evidence_grouped_by_date_and_doc,
    _ns_get_evidence_for_claim,
    _ns_reference_date_to_search_end,
    _ns_timestamp_to_date,
    _ns_validate_parse_and_plan_response,
)
from bigdata_briefs.graph.nodes.novelty_search.fetch_search_evidence import (
    fetch_search_evidence,
)
from bigdata_briefs.graph.nodes.novelty_search.judge_novelty_by_search import (
    judge_novelty_by_search,
)
from bigdata_briefs.graph.nodes.novelty_search.parse_and_plan_search import (
    parse_and_plan_search,
)
from bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets import (
    rewrite_search_bullets,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _make_search_result(
    simple_id: str = "D1-C1",
    doc_id: str = "doc-1",
    chunk_num: int = 1,
    headline: str = "Headline",
    timestamp: str = "2025-01-15T10:00:00",
    source_name: str = "Reuters",
    relevance: float = 0.9,
    chunk_text: str = "Some evidence text.",
) -> _NSSearchResult:
    return _NSSearchResult(
        simple_id=simple_id,
        original_doc_id=doc_id,
        chunk_num=chunk_num,
        headline=headline,
        timestamp=timestamp,
        source_name=source_name,
        relevance=relevance,
        chunk_text=chunk_text,
    )


def _make_parse_response(
    claim_texts: list[str] | None = None,
    query: str = "Apple revenue growth",
) -> _NSParseAndPlanResponse:
    """Build a minimal valid _NSParseAndPlanResponse."""
    if claim_texts is None:
        claim_texts = ["Apple revenue grew 10%."]
    claims = [_NSClaim(text=t) for t in claim_texts]
    sentence_parts = [
        _NSSentencePart(
            text="Apple revenue grew.",
            search_query=query,
            claim_indices=list(range(len(claims))),
        )
    ]
    return _NSParseAndPlanResponse(claims=claims, sentence_parts=sentence_parts)


def _seed_cache_for_parse(deps, trace_id: str, n_claims: int = 1) -> None:
    """Seed the deps._search_cache with parse data for one bullet."""
    claims = [_NSClaim(text=f"Claim {i}") for i in range(n_claims)]
    sentence_parts = [
        _NSSentencePart(
            text="Some text.",
            search_query="Some query",
            claim_indices=list(range(n_claims)),
        )
    ]
    deps.store_search_data(trace_id, "claims", claims)
    deps.store_search_data(trace_id, "sentence_parts", sentence_parts)


def _seed_cache_for_judgment(
    deps,
    trace_id: str,
    n_claims: int = 1,
    with_evidence: bool = True,
) -> None:
    """Seed parse + search results into cache so the judgment node can run."""
    _seed_cache_for_parse(deps, trace_id, n_claims=n_claims)
    if with_evidence:
        result = _make_search_result()
        deps.store_search_data(trace_id, "merged_results", [result])
        deps.store_search_data(trace_id, "results_per_part", [[result]])
    else:
        deps.store_search_data(trace_id, "merged_results", [])
        deps.store_search_data(trace_id, "results_per_part", [[]])


def _seed_cache_for_rewrite(deps, trace_id: str, overall_verdict: str = "novel_with_context") -> None:
    """Seed all intermediate data so the rewrite node can run.

    Defaults to "novel_with_context" so tests that call the LLM path work out of the box.
    Pass overall_verdict="novel" explicitly for tests that exercise the bypass path.
    """
    _seed_cache_for_judgment(deps, trace_id, n_claims=1, with_evidence=True)
    claim_verdicts = [
        _NSClaimVerdict(
            claim_index=0,
            novelty="novel",
            evidence_ids=[],
            reasoning="No prior evidence.",
        )
    ]
    deps.store_search_data(trace_id, "claim_verdicts", claim_verdicts)
    deps.store_search_data(trace_id, "overall_verdict", overall_verdict)


# ══════════════════════════════════════════════════════════════════════════════
# _search_impl: pure utility functions
# ══════════════════════════════════════════════════════════════════════════════


class TestNsTimestampToDate:
    def test_full_iso_timestamp(self):
        assert _ns_timestamp_to_date("2025-12-07T01:18:15") == "2025-12-07"

    def test_date_with_space_separator(self):
        assert _ns_timestamp_to_date("2025-12-07 10:30:00") == "2025-12-07"

    def test_date_only(self):
        assert _ns_timestamp_to_date("2025-12-07") == "2025-12-07"

    def test_empty_string_returns_empty(self):
        assert _ns_timestamp_to_date("") == ""

    def test_none_returns_empty(self):
        assert _ns_timestamp_to_date(None) == ""


class TestNsComputeOverallVerdict:
    def test_all_novel(self):
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "novel"

    def test_all_old(self):
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="old", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "discard_not_new"

    def test_mixed_novel_and_old(self):
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="old", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "novel_with_context"

    def test_partially_novel_single_claim(self):
        # Single partially_novel claim now routes to the dedicated rewriter
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "partial_update"

    def test_partially_novel_multiple_claims(self):
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="partially_novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "multi_partial_update"

    def test_empty_list_returns_old(self):
        assert _ns_compute_overall_verdict([]) == "old"


class TestNsValidateParseAndPlanResponse:
    def _valid_response(self) -> _NSParseAndPlanResponse:
        return _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1"), _NSClaim(text="C2")],
            sentence_parts=[
                _NSSentencePart(text="part 1", search_query="q1", claim_indices=[0]),
                _NSSentencePart(text="part 2", search_query="q2", claim_indices=[1]),
            ],
        )

    def test_valid_response_passes(self):
        _ns_validate_parse_and_plan_response(self._valid_response(), "Apple")

    def test_no_claims_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q", claim_indices=[]),
            ],
        )
        with pytest.raises(ValueError, match="no claims"):
            _ns_validate_parse_and_plan_response(response, "Apple")

    def test_no_sentence_parts_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1")],
            sentence_parts=[],
        )
        with pytest.raises(ValueError, match="no sentence parts"):
            _ns_validate_parse_and_plan_response(response, "Apple")

    def test_invalid_claim_index_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1")],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q", claim_indices=[5]),
            ],
        )
        with pytest.raises(ValueError, match="invalid claim index"):
            _ns_validate_parse_and_plan_response(response, "Apple")

    def test_duplicate_claim_index_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1"), _NSClaim(text="C2")],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q1", claim_indices=[0, 1]),
                _NSSentencePart(text="y", search_query="q2", claim_indices=[0]),
            ],
        )
        with pytest.raises(ValueError, match="assigned to multiple parts"):
            _ns_validate_parse_and_plan_response(response, "Apple")

    def test_missing_claim_assignment_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1"), _NSClaim(text="C2")],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q", claim_indices=[0]),
                # claim index 1 never assigned
            ],
        )
        with pytest.raises(ValueError, match="not assigned to any part"):
            _ns_validate_parse_and_plan_response(response, "Apple")

    def test_duplicate_indices_within_single_part_raises(self):
        response = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1")],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q", claim_indices=[0, 0]),
            ],
        )
        with pytest.raises(ValueError, match="duplicate claim indices"):
            _ns_validate_parse_and_plan_response(response, "Apple")


class TestNsAssignSimpleIds:
    def test_single_result_gets_d1_c1(self):
        raw = [
            {
                "id": "doc-A",
                "headline": "Test",
                "timestamp": "2025-01-01T10:00:00",
                "source": {"name": "Reuters"},
                "url": "",
                "relevance": 0.9,
                "cnum": 1,
                "chunk_text": "Some text.",
                "sentiment": None,
            }
        ]
        results = _ns_assign_simple_ids(raw)
        assert len(results) == 1
        assert results[0].simple_id == "D1-C1"

    def test_multiple_docs_sorted_oldest_first(self):
        raw = [
            {
                "id": "doc-B",
                "headline": "Newer",
                "timestamp": "2025-02-01T00:00:00",
                "source": {"name": "Reuters"},
                "url": "",
                "relevance": 0.9,
                "cnum": 1,
                "chunk_text": "Newer text.",
                "sentiment": None,
            },
            {
                "id": "doc-A",
                "headline": "Older",
                "timestamp": "2025-01-01T00:00:00",
                "source": {"name": "Reuters"},
                "url": "",
                "relevance": 0.8,
                "cnum": 1,
                "chunk_text": "Older text.",
                "sentiment": None,
            },
        ]
        results = _ns_assign_simple_ids(raw)
        assert results[0].simple_id == "D1-C1"
        assert results[1].simple_id == "D2-C1"
        assert results[0].original_doc_id == "doc-A"

    def test_empty_input_returns_empty(self):
        assert _ns_assign_simple_ids([]) == []


class TestNsReferenceDataToSearchEnd:
    def test_date_only_shifts_back_one_day(self):
        result = _ns_reference_date_to_search_end("2025-01-15")
        # 2025-01-14 23:59:59
        assert result == "2025-01-14T23:59:59"

    def test_datetime_input_returned_as_is(self):
        dt = "2025-01-15T12:30:00"
        assert _ns_reference_date_to_search_end(dt) == dt


class TestNsFormatEvidenceGroupedByDateAndDoc:
    def test_empty_returns_empty_string(self):
        assert _ns_format_evidence_grouped_by_date_and_doc([]) == ""

    def test_single_result_formatted_correctly(self):
        r = _make_search_result(
            simple_id="D1-C1",
            doc_id="doc-1",
            headline="Big News",
            timestamp="2025-01-15T10:00:00",
            chunk_text="Something happened.",
        )
        output = _ns_format_evidence_grouped_by_date_and_doc([r])
        assert '2025-01-15 — "Big News"' in output
        assert "[D1-C1] Something happened." in output

    def test_two_results_same_doc_headline_shown_once(self):
        r1 = _make_search_result(simple_id="D1-C1", doc_id="doc-1", timestamp="2025-01-15T10:00:00", chunk_text="Text A.")
        r2 = _make_search_result(simple_id="D1-C2", doc_id="doc-1", timestamp="2025-01-15T11:00:00", chunk_text="Text B.")
        output = _ns_format_evidence_grouped_by_date_and_doc([r1, r2])
        # Headline should appear only once per date-doc group
        assert output.count("Headline") == 1
        assert "[D1-C1] Text A." in output
        assert "[D1-C2] Text B." in output


class TestNsGetEvidenceForClaim:
    def _make_parts(self, n: int) -> list[_NSSentencePart]:
        return [
            _NSSentencePart(
                text=f"part {i}",
                search_query=f"q {i}",
                claim_indices=[i],
            )
            for i in range(n)
        ]

    def test_no_parts_returns_all_results(self):
        results = [_make_search_result(simple_id="D1-C1")]
        output = _ns_get_evidence_for_claim(0, [], [], results)
        assert output == results

    def test_correct_part_evidence_returned(self):
        r0 = _make_search_result(simple_id="D1-C1", chunk_text="For claim 0.")
        r1 = _make_search_result(simple_id="D2-C1", chunk_text="For claim 1.")
        parts = self._make_parts(2)
        results_per_part = [[r0], [r1]]
        all_results = [r0, r1]

        output_0 = _ns_get_evidence_for_claim(0, parts, results_per_part, all_results)
        output_1 = _ns_get_evidence_for_claim(1, parts, results_per_part, all_results)

        assert output_0 == [r0]
        assert output_1 == [r1]

    def test_filters_out_chunks_not_in_merged(self):
        r_merged = _make_search_result(simple_id="D1-C1", chunk_text="In merged.")
        r_not_merged = _make_search_result(simple_id="D2-C1", chunk_text="Not in merged.")
        parts = self._make_parts(1)
        results_per_part = [[r_merged, r_not_merged]]
        all_results = [r_merged]  # r_not_merged excluded from merge

        output = _ns_get_evidence_for_claim(0, parts, results_per_part, all_results)
        assert output == [r_merged]


# ══════════════════════════════════════════════════════════════════════════════
# parse_and_plan_search node
# ══════════════════════════════════════════════════════════════════════════════

_PARSE_MODULE = "bigdata_briefs.graph.nodes.novelty_search.parse_and_plan_search"


class TestParseAndPlanSearch:
    def _call(self, state, deps=None):
        return parse_and_plan_search(state, make_config(deps))

    def test_skips_when_disabled(self):
        state = _state(bullet_points=[make_bullet()])
        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = False
            result = self._call(state)
        m = result["node_metrics"][0]
        assert m["node_id"] == NODE_NOVELTY_SEARCH_PARSE_AND_PLAN
        assert m["extra"]["skipped"] is True

    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[])
        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state)
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_successful_parse_stores_claims_in_cache(self):
        deps = make_deps()
        bp = make_bullet(text="Apple revenue grew 10%.")
        state = _state(bullet_points=[bp], entity_name="Apple")
        parse_resp = _make_parse_response(["Apple revenue grew 10%."])
        deps.llm_client.call_with_response_format.return_value = parse_resp

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        trace_id = bp["trace_id"]
        cached_claims = deps.get_search_data(trace_id, "claims")
        cached_parts = deps.get_search_data(trace_id, "sentence_parts")
        assert cached_claims is not None
        assert len(cached_claims) == 1
        assert cached_parts is not None
        assert len(cached_parts) == 1

    def test_successful_parse_returns_bullets_and_metrics(self):
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp], entity_name="Apple")
        deps.llm_client.call_with_response_format.return_value = _make_parse_response()

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert "bullet_points" in result
        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_SEARCH_PARSE_AND_PLAN
        assert result["node_metrics"][0]["extra"]["bullets_parsed"] == 1
        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 0

    def test_llm_returns_none_deactivates_bullet(self):
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp], entity_name="Apple")
        deps.llm_client.call_with_response_format.return_value = None

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated_bp = result["bullet_points"][0]
        assert updated_bp["is_active"] is False
        assert updated_bp["failure"]["node_id"] == NODE_NOVELTY_SEARCH_PARSE_AND_PLAN
        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 1

    def test_llm_raises_deactivates_bullet(self):
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp], entity_name="Apple")
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("LLM down")

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated_bp = result["bullet_points"][0]
        assert updated_bp["is_active"] is False
        assert updated_bp["failure"]["error_type"] == "RuntimeError"

    def test_inactive_bullets_skipped(self):
        deps = make_deps()
        active = make_bullet(is_active=True)
        inactive = make_bullet(is_active=False)
        state = _state(bullet_points=[active, inactive], entity_name="Apple")
        deps.llm_client.call_with_response_format.return_value = _make_parse_response()

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # LLM called exactly once (for the active bullet)
        assert deps.llm_client.call_with_response_format.call_count == 1

    def test_validation_failure_deactivates_bullet(self):
        """A parse response that fails validation marks the bullet as failed."""
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp], entity_name="Apple")
        # Missing claim assignment — validation will raise
        bad_resp = _NSParseAndPlanResponse(
            claims=[_NSClaim(text="C1"), _NSClaim(text="C2")],
            sentence_parts=[
                _NSSentencePart(text="x", search_query="q", claim_indices=[0])
                # claim 1 unassigned → ValueError
            ],
        )
        deps.llm_client.call_with_response_format.return_value = bad_resp

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is False


# ══════════════════════════════════════════════════════════════════════════════
# fetch_search_evidence node
# ══════════════════════════════════════════════════════════════════════════════

_FETCH_MODULE = "bigdata_briefs.graph.nodes.novelty_search.fetch_search_evidence"


class TestFetchSearchEvidence:
    _EMPTY_SEARCH_RETURN = ([], [], 0, 0.0)

    def _call(self, state, deps=None):
        return fetch_search_evidence(state, make_config(deps))

    def test_skips_when_disabled(self):
        state = _state(bullet_points=[make_bullet()])
        with patch(f"{_FETCH_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = False
            result = self._call(state)
        m = result["node_metrics"][0]
        assert m["node_id"] == NODE_NOVELTY_SEARCH_FETCH
        assert m["extra"]["skipped"] is True

    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[])
        with patch(f"{_FETCH_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state)
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_no_cache_entries(self):
        """Bullets are active but parse cache is empty → should skip."""
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp])
        # No cache seeded → sentence_parts is None

        with patch(f"{_FETCH_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_successful_fetch_stores_results_in_cache(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_parse(deps, trace_id)
        state = _state(
            bullet_points=[bp],
            entity_id="ENT123",
            report_start_date="2025-01-15",
        )

        fake_result = _make_search_result()
        # asyncio.run returns the tuple that _ns_multi_query_search would return
        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(
                f"{_FETCH_MODULE}.asyncio.run",
                return_value=([[fake_result]], [fake_result], 0, 0.5),
            ),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state, deps)

        assert deps.get_search_data(trace_id, "results_per_part") is not None
        assert deps.get_search_data(trace_id, "merged_results") is not None

    def test_returns_only_node_metrics_not_bullet_points(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_parse(deps, bp["trace_id"])
        state = _state(bullet_points=[bp], entity_id="E1", report_start_date="2025-01-15")

        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(f"{_FETCH_MODULE}.asyncio.run", return_value=([], [], 0, 0.0)),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state, deps)

        # fetch node must NOT return bullet_points (intermediate node)
        assert "bullet_points" not in result
        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_SEARCH_FETCH

    def test_search_failure_increments_failed_count_without_deactivating(self):
        """A fetch failure should not deactivate the bullet (judgment handles empty cache)."""
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_parse(deps, bp["trace_id"])
        state = _state(bullet_points=[bp], entity_id="E1", report_start_date="2025-01-15")

        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(f"{_FETCH_MODULE}.asyncio.run", side_effect=RuntimeError("API down")),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state, deps)

        metrics = result["node_metrics"][0]
        assert metrics["extra"]["bullets_failed"] == 1
        # The original bullet must still be active (not touched by fetch node)
        assert bp["is_active"] is True

    def test_metrics_include_query_units(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_parse(deps, bp["trace_id"])
        state = _state(bullet_points=[bp], entity_id="E1", report_start_date="2025-01-15")

        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(f"{_FETCH_MODULE}.asyncio.run", return_value=([], [], 0, 1.25)),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            ms.BIGDATA_API_KEY = "dummy-key"
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["total_query_units"] == 1.25


# ══════════════════════════════════════════════════════════════════════════════
# judge_novelty_by_search node
# ══════════════════════════════════════════════════════════════════════════════

_JUDGE_MODULE = "bigdata_briefs.graph.nodes.novelty_search.judge_novelty_by_search"


class TestJudgeNoveltyBySearch:
    def _call(self, state, deps=None):
        return judge_novelty_by_search(state, make_config(deps))

    def _mock_verdict_response(self, novelty: str = "novel"):
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
            _NSSingleClaimVerdictResponse,
        )
        return _NSSingleClaimVerdictResponse(
            novelty=novelty,
            evidence_ids=[],
            reasoning="Test reasoning.",
        )

    def test_skips_when_disabled(self):
        state = _state(bullet_points=[make_bullet()])
        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = False
            result = self._call(state)
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[])
        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state)
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_no_cache_entries(self):
        """Bullets active but parse cache empty → no entries to judge."""
        deps = make_deps()
        bp = make_bullet()
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_no_evidence_marks_all_claims_novel_without_llm(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_judgment(deps, trace_id, n_claims=2, with_evidence=False)
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # No LLM calls expected
        deps.llm_client.call_with_response_format.assert_not_called()
        verdicts = deps.get_search_data(trace_id, "claim_verdicts")
        assert verdicts is not None
        assert all(v.novelty == "novel" for v in verdicts)
        assert deps.get_search_data(trace_id, "overall_verdict") == "novel"

    def test_with_evidence_calls_llm_per_claim(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_judgment(deps, trace_id, n_claims=2, with_evidence=True)
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")
        deps.llm_client.call_with_response_format.return_value = self._mock_verdict_response("old")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # 2 claims → 2 LLM calls
        assert deps.llm_client.call_with_response_format.call_count == 2

    def test_llm_returns_none_raises_and_increments_failure_count(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_judgment(deps, trace_id, n_claims=1, with_evidence=True)
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")
        deps.llm_client.call_with_response_format.return_value = None

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 1

    def test_verdicts_stored_in_cache(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_judgment(deps, trace_id, n_claims=1, with_evidence=True)
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")
        deps.llm_client.call_with_response_format.return_value = self._mock_verdict_response("novel")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        verdicts = deps.get_search_data(trace_id, "claim_verdicts")
        assert verdicts is not None
        assert verdicts[0].novelty == "novel"
        assert deps.get_search_data(trace_id, "overall_verdict") == "novel"

    def test_returns_only_node_metrics(self):
        deps = make_deps()
        bp = make_bullet()
        trace_id = bp["trace_id"]
        _seed_cache_for_judgment(deps, trace_id, with_evidence=False)
        state = _state(bullet_points=[bp], entity_name="Apple", report_start_date="2025-01-15")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert "bullet_points" not in result
        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_SEARCH_JUDGMENT


# ══════════════════════════════════════════════════════════════════════════════
# rewrite_search_bullets node
# ══════════════════════════════════════════════════════════════════════════════

_REWRITE_MODULE = "bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets"


class TestRewriteSearchBullets:
    def _call(self, state, deps=None):
        return rewrite_search_bullets(state, make_config(deps))

    def _mock_rewrite_response(self, action: str, rewritten: str | None = None):
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSRewriteResponse
        return _NSRewriteResponse(
            action=action,
            rewritten_sentence=rewritten,
            reasoning="Test reasoning.",
        )

    def test_skips_when_disabled_and_clears_cache(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(bullet_points=[bp])

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = False
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["skipped"] is True
        # Cache must be cleared even on skip
        assert deps._search_cache == {}

    def test_skips_when_no_active_bullets_and_clears_cache(self):
        deps = make_deps()
        state = _state(bullet_points=[])

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["node_metrics"][0]["extra"]["skipped"] is True
        assert deps._search_cache == {}

    def test_no_verdict_in_cache_bullet_discarded(self):
        """Bullets with no verdict data in cache are discarded as unverified."""
        deps = make_deps()
        bp = make_bullet()
        # No cache seeded → claim_verdicts is None
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # No LLM call since no cache entries
        deps.llm_client.call_with_response_format.assert_not_called()
        # Bullet should be discarded — novelty check did not complete
        assert result["bullet_points"][0]["is_active"] is False
        assert result["bullet_points"][0]["failure"]["error_type"] == "MissingVerdictData"

    def test_keep_action_preserves_text_and_active(self):
        deps = make_deps()
        bp = make_bullet(text="Original text.")
        _seed_cache_for_rewrite(deps, bp["trace_id"], overall_verdict="novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is True
        assert updated["text"] == "Original text."
        assert updated["novelty_search"]["search"]["verdict"] == "keep"

    def test_discard_action_deactivates_bullet(self):
        """discard_not_new verdict is handled via Python bypass (no LLM call)."""
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"], overall_verdict="discard_not_new")
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is False
        deps.llm_client.call_with_response_format.assert_not_called()
        assert updated["novelty_search"]["search"]["verdict"] == "discard"

    def test_rewrite_action_updates_text(self):
        deps = make_deps()
        bp = make_bullet(text="Original text.")
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response(
            "rewrite", rewritten="Rewritten text."
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is True
        assert updated["text"] == "Rewritten text."
        assert updated["novelty_search"]["search"]["verdict"] == "rewrite"
        assert updated["novelty_search"]["search"]["rewritten_text"] == "Rewritten text."

    def test_llm_returns_none_marks_bullet_as_failed(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = None

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is False
        assert updated["failure"]["node_id"] == NODE_NOVELTY_SEARCH_REWRITE
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_llm_raises_marks_bullet_as_failed(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("LLM error")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is False
        assert updated["failure"]["error_type"] == "RuntimeError"

    def test_cache_cleared_after_rewrite(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response("keep")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        # Cache must be empty after rewrite node completes
        assert deps._search_cache == {}

    def test_debug_logger_called_when_present(self):
        deps = make_deps()
        deps.debug_logger = MagicMock()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response("keep")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        deps.debug_logger.save_novelty_search_langgraph_batch.assert_called_once()

    def test_debug_logger_not_called_when_none(self):
        deps = make_deps()
        deps.debug_logger = None
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response("keep")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # Should complete without error
        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_SEARCH_REWRITE

    def test_returns_bullet_points_and_metrics(self):
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response("keep")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert "bullet_points" in result
        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_SEARCH_REWRITE
        assert result["node_metrics"][0]["extra"]["keep"] == 1
        assert result["node_metrics"][0]["extra"]["discard"] == 0
        assert result["node_metrics"][0]["extra"]["rewrite"] == 0

    def test_inactive_bullets_not_processed(self):
        deps = make_deps()
        active = make_bullet(is_active=True, text="Active text.")
        inactive = make_bullet(is_active=False)
        _seed_cache_for_rewrite(deps, active["trace_id"])
        state = _state(
            bullet_points=[active, inactive],
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = self._mock_rewrite_response("keep")

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        # LLM called only for the active bullet
        assert deps.llm_client.call_with_response_format.call_count == 1
        # Inactive bullet stays as-is
        assert result["bullet_points"][1]["is_active"] is False


# ══════════════════════════════════════════════════════════════════════════════
# Parallel execution tests
#
# Each node uses ThreadPoolExecutor to process bullets concurrently.  These
# tests prove that (a) N bullets are dispatched to N worker threads rather than
# executed one-by-one, and (b) a failure in one worker does not prevent the
# other workers from completing.
#
# Technique: threading.Barrier(N, timeout=5)
#   A barrier blocks until N threads call .wait() simultaneously.
#   If execution were sequential, only one thread would exist at a time and the
#   barrier would never reach N participants → BrokenBarrierError (timeout).
#   With a genuine thread pool the barrier passes cleanly.
# ══════════════════════════════════════════════════════════════════════════════

_BARRIER_TIMEOUT = 5.0  # seconds; generous for CI, tight enough to fail fast


class TestParseAndPlanParallel:
    """parse_and_plan_search dispatches one worker thread per active bullet."""

    N = 4  # number of parallel bullets

    def _run_with_barrier(self, n: int, max_concurrent: int) -> tuple[list[str], dict]:
        """
        Run parse_and_plan_search with ``n`` bullets whose LLM mock blocks on a
        Barrier(n).  Returns (thread_names_seen, node_result).
        """
        barrier = threading.Barrier(n, timeout=_BARRIER_TIMEOUT)
        seen: list[str] = []
        lock = threading.Lock()

        def _mock_llm(*args, **kwargs):
            with lock:
                seen.append(threading.current_thread().name)
            barrier.wait()  # blocks until all n threads arrive here
            return _make_parse_response()

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet(text=f"Bullet {i}.") for i in range(n)]
        state = _state(bullet_points=bullets, entity_name="Apple")

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = max_concurrent
            result = parse_and_plan_search(state, make_config(deps))

        return seen, result

    def test_all_bullets_processed_in_parallel(self):
        seen, result = self._run_with_barrier(self.N, max_concurrent=self.N)
        # Every bullet was processed
        assert result["node_metrics"][0]["extra"]["bullets_parsed"] == self.N
        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 0

    def test_worker_threads_have_expected_name_prefix(self):
        seen, _ = self._run_with_barrier(self.N, max_concurrent=self.N)
        assert len(seen) == self.N
        assert all(name.startswith("ns-parse") for name in seen), (
            f"Unexpected thread names: {seen}"
        )

    def test_all_bullets_ran_on_distinct_threads(self):
        """Each bullet must be dispatched to a separate thread (no reuse across
        barrier waits, confirming true concurrency rather than serial dispatch)."""
        seen, _ = self._run_with_barrier(self.N, max_concurrent=self.N)
        assert len(set(seen)) == self.N, (
            f"Expected {self.N} distinct threads, got {len(set(seen))}: {seen}"
        )

    def test_one_failure_does_not_block_other_bullets(self):
        """When one LLM call raises, the remaining bullets must still complete."""
        n = 3
        call_count = 0
        lock = threading.Lock()
        # Use a barrier of n-1 so the n-1 healthy threads can synchronise even
        # after the failing thread exits early.
        barrier = threading.Barrier(n - 1, timeout=_BARRIER_TIMEOUT)

        def _mock_llm(*args, **kwargs):
            nonlocal call_count
            with lock:
                call_count += 1
                my_count = call_count
            if my_count == 1:
                raise RuntimeError("simulated LLM failure")
            barrier.wait()
            return _make_parse_response()

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet(text=f"Bullet {i}.") for i in range(n)]
        state = _state(bullet_points=bullets, entity_name="Apple")

        with patch(f"{_PARSE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = parse_and_plan_search(state, make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["extra"]["bullets_parsed"] == n - 1
        assert metrics["extra"]["bullets_failed"] == 1


class TestFetchSearchEvidenceParallel:
    """fetch_search_evidence dispatches one worker thread per active bullet."""

    N = 4

    def _run_with_barrier(self, n: int) -> tuple[list[str], dict]:
        barrier = threading.Barrier(n, timeout=_BARRIER_TIMEOUT)
        seen: list[str] = []
        lock = threading.Lock()

        def _mock_asyncio_run(coro):
            # coro is the _ns_multi_query_search coroutine — close it to avoid
            # "coroutine was never awaited" warnings, then do the barrier sync.
            coro.close()
            with lock:
                seen.append(threading.current_thread().name)
            barrier.wait()
            return ([], [], 0, 0.0)

        deps = make_deps()
        bullets = [make_bullet() for _ in range(n)]
        for bp in bullets:
            _seed_cache_for_parse(deps, bp["trace_id"])
        state = _state(
            bullet_points=bullets,
            entity_id="ENT1",
            report_start_date="2025-01-15",
        )

        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(f"{_FETCH_MODULE}.asyncio.run", side_effect=_mock_asyncio_run),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            ms.BIGDATA_API_KEY = "dummy-key"
            result = fetch_search_evidence(state, make_config(deps))

        return seen, result

    def test_all_bullets_fetched_in_parallel(self):
        seen, result = self._run_with_barrier(self.N)
        assert result["node_metrics"][0]["extra"]["bullets_fetched"] == self.N
        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 0

    def test_worker_threads_have_expected_name_prefix(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(seen) == self.N
        assert all(name.startswith("ns-fetch") for name in seen), (
            f"Unexpected thread names: {seen}"
        )

    def test_all_bullets_ran_on_distinct_threads(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(set(seen)) == self.N

    def test_one_fetch_failure_does_not_block_others(self):
        n = 3
        call_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(n - 1, timeout=_BARRIER_TIMEOUT)

        def _mock_asyncio_run(coro):
            nonlocal call_count
            coro.close()
            with lock:
                call_count += 1
                my_count = call_count
            if my_count == 1:
                raise RuntimeError("simulated search failure")
            barrier.wait()
            return ([], [], 0, 0.0)

        deps = make_deps()
        bullets = [make_bullet() for _ in range(n)]
        for bp in bullets:
            _seed_cache_for_parse(deps, bp["trace_id"])
        state = _state(bullet_points=bullets, entity_id="E1", report_start_date="2025-01-15")

        with (
            patch(f"{_FETCH_MODULE}.settings") as ms,
            patch(f"{_FETCH_MODULE}.asyncio.run", side_effect=_mock_asyncio_run),
        ):
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            ms.BIGDATA_API_KEY = "dummy-key"
            result = fetch_search_evidence(state, make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["extra"]["bullets_fetched"] == n - 1
        assert metrics["extra"]["bullets_failed"] == 1


class TestJudgeNoveltyParallel:
    """judge_novelty_by_search dispatches one worker thread per active bullet."""

    N = 4

    def _run_with_barrier(self, n: int) -> tuple[list[str], dict]:
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
            _NSSingleClaimVerdictResponse,
        )

        barrier = threading.Barrier(n, timeout=_BARRIER_TIMEOUT)
        seen: list[str] = []
        lock = threading.Lock()

        def _mock_llm(*args, **kwargs):
            with lock:
                seen.append(threading.current_thread().name)
            barrier.wait()
            return _NSSingleClaimVerdictResponse(
                novelty="novel", evidence_ids=[], reasoning="ok"
            )

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet() for _ in range(n)]
        for bp in bullets:
            # Seed with evidence so the LLM path is taken (not the no-evidence shortcut)
            _seed_cache_for_judgment(deps, bp["trace_id"], n_claims=1, with_evidence=True)
        state = _state(
            bullet_points=bullets,
            entity_name="Apple",
            report_start_date="2025-01-15",
        )

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = judge_novelty_by_search(state, make_config(deps))

        return seen, result

    def test_all_bullets_judged_in_parallel(self):
        seen, result = self._run_with_barrier(self.N)
        assert result["node_metrics"][0]["extra"]["bullets_judged"] == self.N
        assert result["node_metrics"][0]["extra"]["bullets_failed"] == 0

    def test_worker_threads_have_expected_name_prefix(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(seen) == self.N
        assert all(name.startswith("ns-judge") for name in seen), (
            f"Unexpected thread names: {seen}"
        )

    def test_all_bullets_ran_on_distinct_threads(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(set(seen)) == self.N

    def test_one_judgment_failure_does_not_block_others(self):
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
            _NSSingleClaimVerdictResponse,
        )

        n = 3
        call_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(n - 1, timeout=_BARRIER_TIMEOUT)

        def _mock_llm(*args, **kwargs):
            nonlocal call_count
            with lock:
                call_count += 1
                my_count = call_count
            if my_count == 1:
                raise RuntimeError("simulated judgment failure")
            barrier.wait()
            return _NSSingleClaimVerdictResponse(
                novelty="novel", evidence_ids=[], reasoning="ok"
            )

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet() for _ in range(n)]
        for bp in bullets:
            _seed_cache_for_judgment(deps, bp["trace_id"], n_claims=1, with_evidence=True)
        state = _state(bullet_points=bullets, entity_name="Apple", report_start_date="2025-01-15")

        with patch(f"{_JUDGE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = judge_novelty_by_search(state, make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["extra"]["bullets_judged"] == n - 1
        assert metrics["extra"]["bullets_failed"] == 1


class TestRewriteSearchBulletsParallel:
    """rewrite_search_bullets dispatches one worker thread per active bullet."""

    N = 4

    def _run_with_barrier(self, n: int) -> tuple[list[str], dict]:
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSRewriteResponse

        barrier = threading.Barrier(n, timeout=_BARRIER_TIMEOUT)
        seen: list[str] = []
        lock = threading.Lock()

        def _mock_llm(*args, **kwargs):
            with lock:
                seen.append(threading.current_thread().name)
            barrier.wait()
            return _NSRewriteResponse(action="keep", rewritten_sentence=None, reasoning="ok")

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet(text=f"Bullet {i}.") for i in range(n)]
        for bp in bullets:
            _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=bullets,
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = rewrite_search_bullets(state, make_config(deps))

        return seen, result

    def test_all_bullets_rewritten_in_parallel(self):
        seen, result = self._run_with_barrier(self.N)
        assert result["node_metrics"][0]["extra"]["keep"] == self.N
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 0

    def test_worker_threads_have_expected_name_prefix(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(seen) == self.N
        assert all(name.startswith("ns-rewrite") for name in seen), (
            f"Unexpected thread names: {seen}"
        )

    def test_all_bullets_ran_on_distinct_threads(self):
        seen, _ = self._run_with_barrier(self.N)
        assert len(set(seen)) == self.N

    def test_one_rewrite_failure_does_not_block_others(self):
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSRewriteResponse

        n = 3
        call_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(n - 1, timeout=_BARRIER_TIMEOUT)

        def _mock_llm(*args, **kwargs):
            nonlocal call_count
            with lock:
                call_count += 1
                my_count = call_count
            if my_count == 1:
                raise RuntimeError("simulated rewrite failure")
            barrier.wait()
            return _NSRewriteResponse(action="keep", rewritten_sentence=None, reasoning="ok")

        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = _mock_llm
        bullets = [make_bullet(text=f"Bullet {i}.") for i in range(n)]
        for bp in bullets:
            _seed_cache_for_rewrite(deps, bp["trace_id"])
        state = _state(
            bullet_points=bullets,
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = rewrite_search_bullets(state, make_config(deps))

        metrics = result["node_metrics"][0]
        assert metrics["extra"]["keep"] == n - 1
        assert metrics["extra"]["failed_bullets"] == 1

    def test_parallel_results_all_written_to_state(self):
        """All N bullet records must appear in the returned bullet_points with
        novelty_search metadata, regardless of scheduling order."""
        from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSRewriteResponse

        n = self.N
        deps = make_deps()
        bullets = [make_bullet(text=f"Bullet {i}.") for i in range(n)]
        for bp in bullets:
            _seed_cache_for_rewrite(deps, bp["trace_id"], overall_verdict="novel")
        state = _state(
            bullet_points=bullets,
            entity_name="Apple",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = n
            result = rewrite_search_bullets(state, make_config(deps))

        updated = result["bullet_points"]
        assert len(updated) == n
        # Every bullet must have novelty_search metadata
        for bp in updated:
            assert bp["novelty_search"] is not None, (
                f"bullet missing novelty_search: {bp}"
            )
            assert bp["novelty_search"]["search"]["verdict"] == "keep"

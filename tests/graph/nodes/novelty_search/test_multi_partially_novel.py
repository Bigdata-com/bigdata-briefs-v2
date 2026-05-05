"""Tests for the multi_partially_novel rewrite path and related verdict routing.

Covers:
  - _ns_compute_overall_verdict: routing for all partially_novel combinations
  - _ns_build_rewrite_claims_with_reasoning: helper output format
  - rewrite_search_bullets: multi_partially_novel calls LLM with new prompt
  - check_search_rewrite_relevance: multi_partially_novel uses pivot relevance check
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NSClaim,
    _NSClaimVerdict,
    _NSRewriteResponseMixed,
    _NSSentencePart,
    _ns_build_rewrite_claims_with_reasoning,
    _ns_compute_overall_verdict,
)
from bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets import (
    rewrite_search_bullets,
)
from bigdata_briefs.graph.nodes.novelty_search.check_search_rewrite_relevance import (
    score_search_rewrite_relevance,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _verdict(novelty: str, claim_index: int = 0, reasoning: str = "") -> _NSClaimVerdict:
    return _NSClaimVerdict(
        claim_index=claim_index,
        novelty=novelty,
        evidence_ids=[],
        reasoning=reasoning,
    )


def _seed_cache_for_multi_pn(deps, trace_id: str, n_claims: int = 2) -> None:
    claims = [_NSClaim(text=f"Claim {i}.") for i in range(n_claims)]
    deps.store_search_data(trace_id, "claims", claims)
    deps.store_search_data(trace_id, "sentence_parts", [
        _NSSentencePart(text="s", search_query="q", claim_indices=list(range(n_claims)))
    ])
    deps.store_search_data(trace_id, "merged_results", [])
    deps.store_search_data(trace_id, "results_per_part", [[]])
    verdicts = [
        _NSClaimVerdict(
            claim_index=i,
            novelty="partially_novel",
            evidence_ids=[],
            reasoning=f"Topic known, detail {i} is new.",
        )
        for i in range(n_claims)
    ]
    deps.store_search_data(trace_id, "claim_verdicts", verdicts)
    deps.store_search_data(trace_id, "overall_verdict", "multi_partially_novel")


_REWRITE_MODULE = "bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets"
_CHECK_MODULE = "bigdata_briefs.graph.nodes.novelty_search.check_search_rewrite_relevance"


# ── _ns_compute_overall_verdict: all partially_novel combinations ─────────────


class TestComputeOverallVerdictPartiallyNovelCombinations:

    def test_single_pn_no_old_returns_single_partially_novel(self):
        verdicts = [_verdict("partially_novel")]
        assert _ns_compute_overall_verdict(verdicts) == "single_partially_novel"

    def test_single_pn_plus_trivial_returns_single_partially_novel(self):
        """1 pn + trivial noise: len > 1 but pn_count == 1, no old → single_partially_novel."""
        verdicts = [_verdict("partially_novel", 0), _verdict("novel_trivial", 1)]
        assert _ns_compute_overall_verdict(verdicts) == "single_partially_novel"

    def test_single_pn_plus_unsupported_returns_single_partially_novel(self):
        """1 pn + unsupported noise → single_partially_novel (not multi)."""
        verdicts = [_verdict("partially_novel", 0), _verdict("novel_unsupported", 1)]
        assert _ns_compute_overall_verdict(verdicts) == "single_partially_novel"

    def test_two_pn_no_old_returns_multi_partially_novel(self):
        verdicts = [_verdict("partially_novel", 0), _verdict("partially_novel", 1)]
        assert _ns_compute_overall_verdict(verdicts) == "multi_partially_novel"

    def test_three_pn_no_old_returns_multi_partially_novel(self):
        verdicts = [_verdict("partially_novel", i) for i in range(3)]
        assert _ns_compute_overall_verdict(verdicts) == "multi_partially_novel"

    def test_two_pn_plus_trivial_returns_multi_partially_novel(self):
        """2 pn + trivial: pn_count == 2, no old → multi_partially_novel."""
        verdicts = [
            _verdict("partially_novel", 0),
            _verdict("partially_novel", 1),
            _verdict("novel_trivial", 2),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "multi_partially_novel"

    def test_single_pn_plus_old_returns_mixed_partial(self):
        """1 pn + old: has_old is True → mixed_partial."""
        verdicts = [_verdict("partially_novel", 0), _verdict("old", 1)]
        assert _ns_compute_overall_verdict(verdicts) == "mixed_partial"

    def test_two_pn_plus_old_returns_mixed_partial(self):
        """2 pn + old → mixed_partial (old anchor present)."""
        verdicts = [
            _verdict("partially_novel", 0),
            _verdict("partially_novel", 1),
            _verdict("old", 2),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "mixed_partial"

    def test_novel_takes_priority_over_pn(self):
        """novel + pn → mixed, not multi_partially_novel."""
        verdicts = [_verdict("novel", 0), _verdict("partially_novel", 1)]
        assert _ns_compute_overall_verdict(verdicts) == "mixed"

    def test_mixed_weak_no_longer_returned(self):
        """mixed_weak is no longer a valid return value from the aggregator."""
        all_combinations = [
            [_verdict("partially_novel", 0)],
            [_verdict("partially_novel", 0), _verdict("partially_novel", 1)],
            [_verdict("partially_novel", 0), _verdict("old", 1)],
            [_verdict("partially_novel", 0), _verdict("novel_trivial", 1)],
        ]
        for verdicts in all_combinations:
            assert _ns_compute_overall_verdict(verdicts) != "mixed_weak", (
                f"mixed_weak returned for {[v.novelty for v in verdicts]}"
            )


# ── _ns_build_rewrite_claims_with_reasoning ───────────────────────────────────


class TestBuildRewriteClaimsWithReasoning:

    def test_includes_claim_text_verdict_and_reasoning(self):
        claims = [_NSClaim(text="Revenue grew."), _NSClaim(text="Margin expanded.")]
        verdicts = [
            _NSClaimVerdict(
                claim_index=0, novelty="partially_novel",
                evidence_ids=[], reasoning="Prior range was $10–12B; $13B is new.",
            ),
            _NSClaimVerdict(
                claim_index=1, novelty="partially_novel",
                evidence_ids=[], reasoning="Prior margin was 38%; 40.5% is new.",
            ),
        ]
        text = _ns_build_rewrite_claims_with_reasoning(claims, verdicts)
        assert "Revenue grew." in text
        assert "Margin expanded." in text
        assert "partially_novel" in text
        assert "Prior range was $10–12B; $13B is new." in text
        assert "Prior margin was 38%; 40.5% is new." in text
        assert "Judge's analysis:" in text

    def test_fallback_when_reasoning_empty(self):
        claims = [_NSClaim(text="A claim.")]
        verdicts = [_NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning="")]
        text = _ns_build_rewrite_claims_with_reasoning(claims, verdicts)
        assert "no reasoning provided" in text

    def test_empty_returns_empty_string(self):
        assert _ns_build_rewrite_claims_with_reasoning([], []) == ""

    def test_out_of_bounds_claim_index_skipped(self):
        claims = [_NSClaim(text="Only one claim.")]
        verdicts = [
            _NSClaimVerdict(claim_index=5, novelty="partially_novel", evidence_ids=[], reasoning="r"),
        ]
        text = _ns_build_rewrite_claims_with_reasoning(claims, verdicts)
        assert text == ""


# ── rewrite_search_bullets: multi_partially_novel routing ────────────────────


class TestRewriteMultiPartiallyNovel:

    def _call(self, state, deps=None):
        return rewrite_search_bullets(state, make_config(deps))

    def test_multi_pn_calls_llm(self):
        """multi_partially_novel verdict must reach the LLM rewriter."""
        deps = make_deps()
        bp = make_bullet(text="Intel reported EPS of $0.13 and revenue of $12.67B.")
        _seed_cache_for_multi_pn(deps, bp["trace_id"], n_claims=2)
        state = _state(
            bullet_points=[bp],
            entity_name="Intel Corp.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Intel Corp., which had been expected to report near consensus, has now reported EPS of $0.13 and revenue of $12.67B.",
            reasoning="Known: analyst estimates. New: both specific figures.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        deps.llm_client.call_with_response_format.assert_called_once()

    def test_multi_pn_prompt_contains_claims_with_reasoning(self):
        """The prompt sent to the LLM must include per-claim reasoning (not just verdict labels)."""
        deps = make_deps()
        bp = make_bullet(text="Two claims.")
        trace_id = bp["trace_id"]
        deps.store_search_data(trace_id, "claims", [
            _NSClaim(text="EPS claim."), _NSClaim(text="Revenue claim."),
        ])
        deps.store_search_data(trace_id, "sentence_parts", [
            _NSSentencePart(text="s", search_query="q", claim_indices=[0, 1])
        ])
        deps.store_search_data(trace_id, "merged_results", [])
        deps.store_search_data(trace_id, "results_per_part", [[]])
        deps.store_search_data(trace_id, "claim_verdicts", [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning="EPS range was $0.08-0.10."),
            _NSClaimVerdict(claim_index=1, novelty="partially_novel", evidence_ids=[], reasoning="Revenue range was $12.2-12.5B."),
        ])
        deps.store_search_data(trace_id, "overall_verdict", "multi_partially_novel")

        state = _state(
            bullet_points=[bp],
            entity_name="Intel Corp.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Intel Corp., which had been expected ..., has now reported ...",
            reasoning="ok.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        user_msg = deps.llm_client.call_with_response_format.call_args[1]["messages"][0]["content"]
        assert "Judge's analysis:" in user_msg
        assert "EPS range was $0.08-0.10." in user_msg
        assert "Revenue range was $12.2-12.5B." in user_msg

    def test_multi_pn_rewrite_updates_text_and_stays_active(self):
        deps = make_deps()
        bp = make_bullet(text="Original.")
        _seed_cache_for_multi_pn(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Intel Corp.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Intel Corp., which had been expected near consensus, has now reported EPS $0.13 and revenue $12.67B.",
            reasoning="ok.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is True
        assert "has now reported" in updated["text"]
        assert updated["novelty_search"]["search"]["verdict"] == "rewrite"
        assert updated["novelty_search"]["search"]["overall_verdict"] == "multi_partially_novel"

    def test_multi_pn_not_discarded_without_llm(self):
        """multi_partially_novel must NOT be bypassed as a Python-level discard."""
        deps = make_deps()
        bp = make_bullet()
        _seed_cache_for_multi_pn(deps, bp["trace_id"])
        state = _state(
            bullet_points=[bp],
            entity_name="Intel Corp.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Rewritten.", reasoning="ok."
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is True
        deps.llm_client.call_with_response_format.assert_called_once()

    def test_single_pn_plus_trivial_routes_to_single_pn_not_multi(self):
        """1 pn + trivial: should use single_partially_novel path, not multi_partially_novel."""
        deps = make_deps()
        bp = make_bullet(text="One real claim plus noise.")
        trace_id = bp["trace_id"]
        reasoning = "Known: topic X. New: specific figure Y."
        deps.store_search_data(trace_id, "claims", [
            _NSClaim(text="Real claim."), _NSClaim(text="Trivial claim."),
        ])
        deps.store_search_data(trace_id, "sentence_parts", [
            _NSSentencePart(text="s", search_query="q", claim_indices=[0, 1])
        ])
        deps.store_search_data(trace_id, "merged_results", [])
        deps.store_search_data(trace_id, "results_per_part", [[]])
        deps.store_search_data(trace_id, "claim_verdicts", [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=reasoning),
            _NSClaimVerdict(claim_index=1, novelty="novel_trivial", evidence_ids=[], reasoning="Trivial."),
        ])
        deps.store_search_data(trace_id, "overall_verdict", "single_partially_novel")

        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Corp Inc., which had X, has now disclosed Y.",
            reasoning="ok.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        user_msg = deps.llm_client.call_with_response_format.call_args[1]["messages"][0]["content"]
        # single_partially_novel passes judge reasoning as {reasoning}, not claims_with_reasoning.
        # multi_partially_novel would include "Verdict 1:" labels; single_pn does not.
        assert reasoning in user_msg
        assert "Verdict 1:" not in user_msg


# ── check_search_rewrite_relevance: multi_partially_novel uses pivot check ────


class TestCheckSearchRewriteRelevanceMultiPN:

    def _make_bp_with_verdict(self, overall_verdict: str) -> dict:
        bp = make_bullet(text="Original.")
        bp["is_active"] = True
        bp["novelty_search"] = {
            "search": {
                "verdict": "rewrite",
                "rewritten_text": "Corp Inc., which had X, has now reported Y and Z.",
                "overall_verdict": overall_verdict,
                "reason": "ok",
                "duration_seconds": 0.0,
            },
            "relevance_check": None,
        }
        return bp

    def _call(self, state, deps=None):
        return score_search_rewrite_relevance(state, make_config(deps))

    def test_multi_pn_uses_pivot_relevance_check(self):
        """multi_partially_novel rewrite → run_pivot_relevance_check, not general."""
        deps = make_deps()
        bp = self._make_bp_with_verdict("multi_partially_novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(4, "Material.")) as mock_pivot, \
             patch(f"{_CHECK_MODULE}.run_relevance_check_for_bullet_text") as mock_general, \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            self._call(state, deps)

        mock_pivot.assert_called_once()
        mock_general.assert_not_called()

    def test_multi_pn_low_score_deactivates_bullet(self):
        deps = make_deps()
        bp = self._make_bp_with_verdict("multi_partially_novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(1, "Not material.")), \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is False

    def test_multi_pn_high_score_keeps_bullet_active(self):
        deps = make_deps()
        bp = self._make_bp_with_verdict("multi_partially_novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(5, "Highly material.")), \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is True

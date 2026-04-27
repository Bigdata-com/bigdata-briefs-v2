"""Tests for the single_partially_novel rewrite path.

Covers:
  - _ns_compute_overall_verdict routing (single vs multiple partially_novel)
  - rewrite_search_bullets: single_partially_novel uses new prompt, mixed_weak still discarded
  - run_pivot_relevance_check: calls LLM with pivot prompt, returns score
  - check_search_rewrite_relevance: pivot verdicts use run_pivot_relevance_check
  - Existing test fix: single partially_novel now returns single_partially_novel, not mixed
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NSClaim,
    _NSClaimVerdict,
    _NSPivotRelevanceResult,
    _NSRewriteResponseMixed,
    _REWRITE_PROMPT_SINGLE_PARTIALLY_NOVEL,
    _ns_compute_overall_verdict,
    run_pivot_relevance_check,
)
from bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets import (
    rewrite_search_bullets,
)
from bigdata_briefs.graph.nodes.novelty_search.check_search_rewrite_relevance import (
    score_search_rewrite_relevance,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _pn_verdict(reasoning: str = "Topic known, figure new.") -> _NSClaimVerdict:
    return _NSClaimVerdict(
        claim_index=0,
        novelty="partially_novel",
        evidence_ids=["D1-C1"],
        reasoning=reasoning,
    )


def _seed_cache_for_single_pn(deps, trace_id: str, reasoning: str = "Topic known, figure new.") -> None:
    deps.store_search_data(trace_id, "claims", [_NSClaim(text="A claim.")])
    from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSSentencePart
    deps.store_search_data(trace_id, "sentence_parts", [
        _NSSentencePart(text="sentence", search_query="q", claim_indices=[0])
    ])
    deps.store_search_data(trace_id, "merged_results", [])
    deps.store_search_data(trace_id, "results_per_part", [[]])
    deps.store_search_data(trace_id, "claim_verdicts", [_pn_verdict(reasoning)])
    deps.store_search_data(trace_id, "overall_verdict", "single_partially_novel")


_REWRITE_MODULE = "bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets"
_CHECK_MODULE = "bigdata_briefs.graph.nodes.novelty_search.check_search_rewrite_relevance"


# ── _ns_compute_overall_verdict ───────────────────────────────────────────────


class TestComputeOverallVerdictSinglePartiallyNovel:

    def test_single_partially_novel_claim_returns_new_verdict(self):
        verdicts = [_NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning="")]
        assert _ns_compute_overall_verdict(verdicts) == "single_partially_novel"

    def test_two_partially_novel_claims_returns_mixed_weak(self):
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="partially_novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "mixed_weak"

    def test_partially_novel_plus_old_returns_mixed_weak(self):
        """Multiple claims, one partially_novel, one old → mixed_weak (no novel to anchor a pivot)."""
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="old", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "mixed_weak"

    def test_novel_takes_priority_over_partially_novel(self):
        """novel + partially_novel → mixed (not single_partially_novel)."""
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="partially_novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "mixed"

    def test_single_partially_novel_plus_trivial_still_novel_path(self):
        """If there's a novel claim, partially_novel doesn't trigger single_partially_novel."""
        verdicts = [
            _NSClaimVerdict(claim_index=0, novelty="novel", evidence_ids=[], reasoning=""),
        ]
        assert _ns_compute_overall_verdict(verdicts) == "novel"


# ── rewrite_search_bullets: single_partially_novel routing ───────────────────


class TestRewriteSinglePartiallyNovel:

    def _call(self, state, deps=None):
        return rewrite_search_bullets(state, make_config(deps))

    def test_single_partially_novel_calls_llm_with_pivot_prompt(self):
        """single_partially_novel verdict must reach the LLM (not be bypassed as discard)."""
        deps = make_deps()
        bp = make_bullet(text="UnitedHealth posted Q1 pre-tax profit of $8.04B.")
        reasoning = "Q1 results topic known, $8.04B figure is new."
        _seed_cache_for_single_pn(deps, bp["trace_id"], reasoning=reasoning)

        state = _state(
            bullet_points=[bp],
            entity_name="UnitedHealth Group Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="UnitedHealth, which reported Q1 earnings from ops of $9.0B, has now disclosed a pre-tax profit of $8.04B.",
            reasoning="Known: Q1 ops earnings. New: pre-tax $8.04B.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        deps.llm_client.call_with_response_format.assert_called_once()
        call_kwargs = deps.llm_client.call_with_response_format.call_args
        # The prompt content must contain the pivot prompt marker text
        user_msg = call_kwargs[1]["messages"][0]["content"]
        assert "pivot marker" in user_msg
        assert reasoning in user_msg

    def test_single_partially_novel_prompt_contains_reasoning_not_claim_labels(self):
        """single_partially_novel passes judge reasoning, not old/novel claim labels."""
        deps = make_deps()
        bp = make_bullet()
        reasoning = "The specific figure $8.04B is not in evidence."
        _seed_cache_for_single_pn(deps, bp["trace_id"], reasoning=reasoning)

        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )
        deps.llm_client.call_with_response_format.return_value = _NSRewriteResponseMixed(
            rewritten_sentence="Corp Inc., which did X, has now reported Y.",
            reasoning="known/new split.",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            self._call(state, deps)

        user_msg = deps.llm_client.call_with_response_format.call_args[1]["messages"][0]["content"]
        # Reasoning is in the prompt; claim labels (old/novel) are NOT
        assert reasoning in user_msg
        assert "Verdict 1:" not in user_msg

    def test_single_partially_novel_rewrite_updates_text_and_stays_active(self):
        deps = make_deps()
        bp = make_bullet(text="Original.")
        _seed_cache_for_single_pn(deps, bp["trace_id"])
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
            result = self._call(state, deps)

        updated = result["bullet_points"][0]
        assert updated["is_active"] is True
        assert updated["text"] == "Corp Inc., which had X, has now disclosed Y."
        assert updated["novelty_search"]["search"]["verdict"] == "rewrite"
        assert updated["novelty_search"]["search"]["overall_verdict"] == "single_partially_novel"

    def test_mixed_weak_multi_claim_still_discarded(self):
        """mixed_weak (multiple partially_novel) must still be discarded without LLM call."""
        deps = make_deps()
        bp = make_bullet()

        from bigdata_briefs.graph.nodes.novelty_search._search_impl import _NSSentencePart
        deps.store_search_data(bp["trace_id"], "claims", [
            _NSClaim(text="Claim A."), _NSClaim(text="Claim B.")
        ])
        deps.store_search_data(bp["trace_id"], "sentence_parts", [
            _NSSentencePart(text="s", search_query="q", claim_indices=[0, 1])
        ])
        deps.store_search_data(bp["trace_id"], "merged_results", [])
        deps.store_search_data(bp["trace_id"], "results_per_part", [[]])
        deps.store_search_data(bp["trace_id"], "claim_verdicts", [
            _NSClaimVerdict(claim_index=0, novelty="partially_novel", evidence_ids=[], reasoning=""),
            _NSClaimVerdict(claim_index=1, novelty="partially_novel", evidence_ids=[], reasoning=""),
        ])
        deps.store_search_data(bp["trace_id"], "overall_verdict", "mixed_weak")

        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_REWRITE_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_ENABLED = True
            ms.NOVELTY_SEARCH_MAX_CONCURRENT = 4
            result = self._call(state, deps)

        deps.llm_client.call_with_response_format.assert_not_called()
        assert result["bullet_points"][0]["is_active"] is False


# ── run_pivot_relevance_check ─────────────────────────────────────────────────


class TestRunPivotRelevanceCheck:

    def _mock_llm(self, score: int = 4, reason: str = "Material.") -> MagicMock:
        llm = MagicMock()
        llm.call_with_response_format.return_value = _NSPivotRelevanceResult(
            relevance_score=score, reason=reason
        )
        return llm

    def test_returns_score_and_reason(self):
        llm = self._mock_llm(score=4, reason="New figure is actionable.")
        score, reason = run_pivot_relevance_check(
            rewritten_sentence="Corp Inc., which had X, has now reported Y.",
            entity_name="Corp Inc.",
            llm_client=llm,
            step_name="test_step",
        )
        assert score == 4
        assert reason == "New figure is actionable."

    def test_prompt_contains_entity_name_and_sentence(self):
        llm = self._mock_llm()
        run_pivot_relevance_check(
            rewritten_sentence="Corp Inc., which had X, has now reported $8B.",
            entity_name="Corp Inc.",
            llm_client=llm,
            step_name="test_step",
        )
        user_msg = llm.call_with_response_format.call_args[1]["messages"][0]["content"]
        assert "Corp Inc." in user_msg
        assert "$8B" in user_msg
        assert "pivot marker" in user_msg

    def test_llm_failure_returns_default_score(self):
        llm = MagicMock()
        llm.call_with_response_format.side_effect = RuntimeError("LLM error")
        score, reason = run_pivot_relevance_check(
            rewritten_sentence="Corp Inc., which had X, has now reported Y.",
            entity_name="Corp Inc.",
            llm_client=llm,
            step_name="test_step",
            default_score=4,
        )
        assert score == 4
        assert reason is None

    def test_custom_default_score_on_failure(self):
        llm = MagicMock()
        llm.call_with_response_format.side_effect = ValueError("bad")
        score, _ = run_pivot_relevance_check(
            rewritten_sentence="X",
            entity_name="Corp",
            llm_client=llm,
            step_name="s",
            default_score=3,
        )
        assert score == 3


# ── check_search_rewrite_relevance: routing ───────────────────────────────────


class TestCheckSearchRewriteRelevanceRouting:

    def _make_bp_with_search_verdict(
        self,
        rewritten_text: str = "Corp Inc., which had X, has now reported Y.",
        overall_verdict: str = "mixed",
    ) -> dict:
        bp = make_bullet(text="Original.")
        bp["is_active"] = True
        bp["novelty_search"] = {
            "search": {
                "verdict": "rewrite",
                "rewritten_text": rewritten_text,
                "overall_verdict": overall_verdict,
                "reason": "ok",
                "duration_seconds": 0.0,
            },
            "relevance_check": None,
        }
        return bp

    def _call(self, state, deps=None):
        return score_search_rewrite_relevance(state, make_config(deps))

    def test_pivot_verdict_uses_pivot_check(self):
        """mixed verdict → run_pivot_relevance_check, not the general one."""
        deps = make_deps()
        bp = self._make_bp_with_search_verdict(overall_verdict="mixed")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        pivot_result = _NSPivotRelevanceResult(relevance_score=4, reason="Material.")
        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(4, "Material.")) as mock_pivot, \
             patch(f"{_CHECK_MODULE}.run_relevance_check_for_bullet_text") as mock_general, \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            self._call(state, deps)

        mock_pivot.assert_called_once()
        mock_general.assert_not_called()

    def test_single_partially_novel_verdict_uses_pivot_check(self):
        """single_partially_novel → run_pivot_relevance_check."""
        deps = make_deps()
        bp = self._make_bp_with_search_verdict(overall_verdict="single_partially_novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(4, "ok.")) as mock_pivot, \
             patch(f"{_CHECK_MODULE}.run_relevance_check_for_bullet_text") as mock_general, \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            self._call(state, deps)

        mock_pivot.assert_called_once()
        mock_general.assert_not_called()

    def test_mixed_noise_verdict_uses_general_check(self):
        """mixed_noise is not a pivot verdict → general relevance check."""
        deps = make_deps()
        bp = self._make_bp_with_search_verdict(overall_verdict="mixed_noise")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check") as mock_pivot, \
             patch(f"{_CHECK_MODULE}.run_relevance_check_for_bullet_text", return_value=(4, "ok.")) as mock_general, \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            self._call(state, deps)

        mock_pivot.assert_not_called()
        mock_general.assert_called_once()

    def test_low_pivot_score_deactivates_bullet(self):
        deps = make_deps()
        bp = self._make_bp_with_search_verdict(overall_verdict="mixed")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(2, "Not material.")), \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is False

    def test_high_pivot_score_keeps_bullet_active(self):
        deps = make_deps()
        bp = self._make_bp_with_search_verdict(overall_verdict="single_partially_novel")
        state = _state(
            bullet_points=[bp],
            entity_name="Corp Inc.",
            entity_id="E1",
            report_start_date="2025-01-15",
        )

        with patch(f"{_CHECK_MODULE}.run_pivot_relevance_check", return_value=(4, "Material.")), \
             patch(f"{_CHECK_MODULE}.settings") as ms:
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED = True
            ms.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT = 4
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 3
            result = self._call(state, deps)

        assert result["bullet_points"][0]["is_active"] is True

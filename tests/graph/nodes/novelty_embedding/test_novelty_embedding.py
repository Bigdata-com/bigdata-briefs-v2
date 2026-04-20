"""Tests for all novelty embedding nodes (embed, judge, rewrite, relevance)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_EMBED_AND_RETRIEVE,
    NODE_NOVELTY_JUDGMENT_EMBEDDING,
    NODE_RELEVANCE_CHECK_EMBEDDING,
    NODE_REWRITE_EMBEDDING,
)
from bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance import (
    score_embedding_rewrite_relevance,
)
from bigdata_briefs.graph.nodes.novelty_embedding.embed_and_retrieve_candidates import (
    compute_embeddings_and_retrieve_candidates,
)
from bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding import (
    evaluate_novelty_by_embedding_similarity,
)
from bigdata_briefs.graph.nodes.novelty_embedding.rewrite_non_novel_bullets import (
    rewrite_partially_novel_bullets,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _llm_result(decision="KEEP", reason="ok", evaluator_details=None):
    r = MagicMock()
    r.decision = decision
    r.reason = reason
    r.evaluator_details = evaluator_details or []
    return r


# ══════════════════════════════════════════════════════════════════════════════
# compute_embeddings_and_retrieve_candidates (embed_and_retrieve)
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeEmbeddingsAndRetrieveCandidates:
    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[])
        result = compute_embeddings_and_retrieve_candidates(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_vectors_cached_by_trace_id(self):
        deps = make_deps()
        deps.novelty_service._normalize_text_for_embedding.return_value = "normalised text"
        deps.novelty_service.embedding_client.compute.return_value = [[0.1, 0.2, 0.3]]

        bp = make_bullet(text="Earnings grew 10%.")
        state = _state(bullet_points=[bp])
        compute_embeddings_and_retrieve_candidates(state, make_config(deps))

        trace_id = bp["trace_id"]
        cached = deps.get_embedding(trace_id)
        assert cached == [0.1, 0.2, 0.3]

    def test_inactive_bullets_skipped(self):
        deps = make_deps()
        deps.novelty_service.embedding_client.compute.return_value = []

        active = make_bullet(is_active=True)
        inactive = make_bullet(is_active=False)
        state = _state(bullet_points=[active, inactive])
        compute_embeddings_and_retrieve_candidates(state, make_config(deps))

        # Only one bullet was embedded
        assert deps.novelty_service.embedding_client.compute.call_args.args[0].__len__() == 1

    def test_returns_only_node_metrics(self):
        deps = make_deps()
        deps.novelty_service._normalize_text_for_embedding.return_value = "text"
        deps.novelty_service.embedding_client.compute.return_value = [[0.5]]

        bp = make_bullet()
        result = compute_embeddings_and_retrieve_candidates(_state(bullet_points=[bp]), make_config(deps))

        assert set(result.keys()) == {"node_metrics"}
        assert result["node_metrics"][0]["node_id"] == NODE_EMBED_AND_RETRIEVE

    def test_embedding_exception_propagates(self):
        deps = make_deps()
        deps.novelty_service._normalize_text_for_embedding.return_value = "text"
        deps.novelty_service.embedding_client.compute.side_effect = RuntimeError("embed API down")

        with pytest.raises(RuntimeError, match="embed API down"):
            compute_embeddings_and_retrieve_candidates(_state(bullet_points=[make_bullet()]), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# evaluate_novelty_by_embedding_similarity (novelty_judgment_embedding)
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateNoveltyByEmbeddingSimilarity:
    def _mock_novelty_step(self, deps, decisions: list[str]):
        results = [_llm_result(decision=d) for d in decisions]
        embeddings = [[0.1, 0.2]] * len(decisions)
        deps.novelty_service.novelty_embedding_step.return_value = (
            [],        # kept texts (unused)
            results,   # llm_results
            embeddings,  # all_embeddings
            [],        # deferred
        )

    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[])
        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            result = evaluate_novelty_by_embedding_similarity(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_keep_decision_preserves_active(self):
        deps = make_deps()
        self._mock_novelty_step(deps, ["KEEP"])
        bp = make_bullet()
        state = _state(bullet_points=[bp])

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            result = evaluate_novelty_by_embedding_similarity(state, make_config(deps))

        updated_bp = result["bullet_points"][0]
        assert updated_bp["is_active"] is True
        assert updated_bp["novelty_embedding"]["judgment"]["decision"] == "keep"

    def test_discard_decision_deactivates_bullet(self):
        deps = make_deps()
        self._mock_novelty_step(deps, ["DISCARD"])
        state = _state(bullet_points=[make_bullet()])

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            result = evaluate_novelty_by_embedding_similarity(state, make_config(deps))

        assert result["bullet_points"][0]["is_active"] is False

    def test_rewrite_decision_keeps_active(self):
        deps = make_deps()
        self._mock_novelty_step(deps, ["REWRITE"])
        state = _state(bullet_points=[make_bullet()])

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            result = evaluate_novelty_by_embedding_similarity(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is True
        assert bp["novelty_embedding"]["judgment"]["decision"] == "rewrite"

    def test_embeddings_written_back_to_cache(self):
        deps = make_deps()
        self._mock_novelty_step(deps, ["KEEP"])
        bp = make_bullet()
        state = _state(bullet_points=[bp])

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            evaluate_novelty_by_embedding_similarity(state, make_config(deps))

        cached = deps.get_embedding(bp["trace_id"])
        assert cached == [0.1, 0.2]

    def test_returns_node_metrics(self):
        deps = make_deps()
        self._mock_novelty_step(deps, ["KEEP"])

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding.make_three_window_evaluators", return_value=([], MagicMock())):
            result = evaluate_novelty_by_embedding_similarity(_state(bullet_points=[make_bullet()]), make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_NOVELTY_JUDGMENT_EMBEDDING


# ══════════════════════════════════════════════════════════════════════════════
# rewrite_partially_novel_bullets (rewrite_embedding)
# ══════════════════════════════════════════════════════════════════════════════

class TestRewritePartiallyNovelBullets:
    def _bullet_flagged_for_rewrite(self):
        bp = make_bullet(text="Original text with old facts.")
        bp["novelty_embedding"] = {
            "judgment": {
                "decision": "rewrite",
                "reason": "partially novel",
                "evaluator_details": [{"decision": "rewrite", "instruction": "Remove old part."}],
            }
        }
        return bp

    def test_skips_when_no_rewrite_bullets(self):
        bp = make_bullet()
        state = _state(bullet_points=[bp])  # no REWRITE judgment
        result = rewrite_partially_novel_bullets(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_successful_rewrite_updates_text(self):
        deps = make_deps()
        judge_mock = MagicMock()
        judge_mock.run_step2_rewrite.return_value = ("Rewritten novel text.", "KEEP")

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.rewrite_non_novel_bullets.LLMNoveltyJudge", return_value=judge_mock):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()])
            result = rewrite_partially_novel_bullets(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["text"] == "Rewritten novel text."
        assert bp["is_active"] is True
        assert bp["novelty_embedding"]["rewrite"]["text_after"] == "Rewritten novel text."

    def test_empty_rewrite_deactivates_bullet(self):
        deps = make_deps()
        judge_mock = MagicMock()
        judge_mock.run_step2_rewrite.return_value = ("", "DISCARD")

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.rewrite_non_novel_bullets.LLMNoveltyJudge", return_value=judge_mock):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()])
            result = rewrite_partially_novel_bullets(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["novelty_embedding"]["rewrite"]["is_empty"] is True

    def test_llm_failure_marks_bullet_with_failure_record(self):
        deps = make_deps()
        judge_mock = MagicMock()
        judge_mock.run_step2_rewrite.side_effect = RuntimeError("LLM unavailable")

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.rewrite_non_novel_bullets.LLMNoveltyJudge", return_value=judge_mock):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()])
            result = rewrite_partially_novel_bullets(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["failure"]["node_id"] == NODE_REWRITE_EMBEDDING
        assert bp["failure"]["error_type"] == "RuntimeError"
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_returns_node_metrics(self):
        deps = make_deps()
        judge_mock = MagicMock()
        judge_mock.run_step2_rewrite.return_value = ("Rewritten.", "KEEP")

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.rewrite_non_novel_bullets.LLMNoveltyJudge", return_value=judge_mock):
            result = rewrite_partially_novel_bullets(
                _state(bullet_points=[self._bullet_flagged_for_rewrite()]),
                make_config(deps),
            )

        assert result["node_metrics"][0]["node_id"] == NODE_REWRITE_EMBEDDING


# ══════════════════════════════════════════════════════════════════════════════
# score_embedding_rewrite_relevance (relevance_check_embedding)
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreEmbeddingRewriteRelevance:
    def _bullet_with_rewrite(self, text_after="Rewritten text."):
        bp = make_bullet()
        bp["novelty_embedding"] = {
            "rewrite": {"text_before": "Original.", "text_after": text_after, "is_empty": False}
        }
        return bp

    def test_skips_when_no_rewritten_bullets(self):
        state = _state(bullet_points=[make_bullet()])  # no rewrite
        result = score_embedding_rewrite_relevance(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_passing_score_keeps_bullet_active(self):
        deps = make_deps()
        deps.novelty_service  # unused

        with (
            patch("bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance._run_relevance_check_on_rewrite", return_value=5),
            patch("bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance.settings") as ms,
        ):
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = score_embedding_rewrite_relevance(
                _state(bullet_points=[self._bullet_with_rewrite()]),
                make_config(deps),
            )

        bp = result["bullet_points"][0]
        assert bp["is_active"] is True
        assert bp["novelty_embedding"]["relevance_check"]["passed"] is True

    def test_failing_score_deactivates_bullet(self):
        deps = make_deps()

        with (
            patch("bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance._run_relevance_check_on_rewrite", return_value=1),
            patch("bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance.settings") as ms,
        ):
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = score_embedding_rewrite_relevance(
                _state(bullet_points=[self._bullet_with_rewrite()]),
                make_config(deps),
            )

        assert result["bullet_points"][0]["is_active"] is False

    def test_llm_failure_marks_bullet_with_failure_record(self):
        deps = make_deps()

        with patch(
            "bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance._run_relevance_check_on_rewrite",
            side_effect=RuntimeError("LLM down"),
        ):
            result = score_embedding_rewrite_relevance(
                _state(bullet_points=[self._bullet_with_rewrite()]),
                make_config(deps),
            )

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["failure"]["node_id"] == NODE_RELEVANCE_CHECK_EMBEDDING
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_returns_node_metrics(self):
        deps = make_deps()

        with patch("bigdata_briefs.graph.nodes.novelty_embedding.check_rewrite_relevance._run_relevance_check_on_rewrite", return_value=5):
            result = score_embedding_rewrite_relevance(
                _state(bullet_points=[self._bullet_with_rewrite()]),
                make_config(deps),
            )

        assert result["node_metrics"][0]["node_id"] == NODE_RELEVANCE_CHECK_EMBEDDING

"""Tests for Phase 2 bullet generation and relevance scoring nodes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import NODE_BULLETS_GENERATION, NODE_RELEVANCE_SCORE
from bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets import (
    produce_bullets_for_theme,
)
from bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance import (
    score_and_gate_bullet_relevance,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


# ── Prompt mock helper ────────────────────────────────────────────────────────

def _mock_prompt_keys():
    pk = MagicMock()
    pk.system_prompt = "system"
    pk.user_template.render.return_value = "user content"
    pk.llm_kwargs = {}
    return pk


# ══════════════════════════════════════════════════════════════════════════════
# produce_bullets_for_theme (bullets_generation)
# ══════════════════════════════════════════════════════════════════════════════

class TestProduceBulletsForTheme:
    def _base_state(self):
        return _state(
            themes=["Revenue"],
            active_theme_index=0,
            extracted_concepts={"categories": [{"theme": "Revenue", "concepts": ["earnings"]}]},
            processed_concept_results={
                "results_by_theme": {"Revenue": []},
            },
        )

    def test_skips_when_theme_index_out_of_range(self):
        state = self._base_state()
        state["active_theme_index"] = 99
        result = produce_bullets_for_theme(state, make_config())
        assert "bullet_points" not in result
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_appends_new_bullets_to_existing(self):
        deps = make_deps()
        existing = make_bullet(text="Old bullet", theme="Theme B")

        mock_collection = MagicMock()
        deps.llm_client.call_with_response_format.return_value = mock_collection

        with (
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.create_sources_for_results", return_value=(MagicMock(), {})),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.replace_references_in_topic_collection_no_score", return_value=mock_collection),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.process_topic_collection_no_score", return_value=(["New bullet."], [["CQS:REF0"]])),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_prompt_keys", return_value=_mock_prompt_keys()),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_iterative_theme_user_prompt", return_value="prompt"),
        ):
            state = {**self._base_state(), "bullet_points": [existing]}
            result = produce_bullets_for_theme(state, make_config(deps))

        # Old bullet preserved + 1 new
        assert len(result["bullet_points"]) == 2
        new_texts = [bp["text"] for bp in result["bullet_points"]]
        assert "Old bullet" in new_texts
        assert "New bullet." in new_texts

    def test_new_bullets_have_generation_metadata(self):
        deps = make_deps()
        mock_collection = MagicMock()
        deps.llm_client.call_with_response_format.return_value = mock_collection

        with (
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.create_sources_for_results", return_value=(MagicMock(), {})),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.replace_references_in_topic_collection_no_score", return_value=mock_collection),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.process_topic_collection_no_score", return_value=(["Bullet text."], [["ref1"]])),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_prompt_keys", return_value=_mock_prompt_keys()),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_iterative_theme_user_prompt", return_value="prompt"),
        ):
            result = produce_bullets_for_theme(self._base_state(), make_config(deps))

        new_bp = result["bullet_points"][0]
        assert new_bp["generation"] is not None
        assert new_bp["generation"]["theme_name"] == "Revenue"
        assert new_bp["theme"] == "Revenue"

    def test_returns_node_metrics(self):
        deps = make_deps()
        mock_collection = MagicMock()
        deps.llm_client.call_with_response_format.return_value = mock_collection

        with (
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.create_sources_for_results", return_value=(MagicMock(), {})),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.replace_references_in_topic_collection_no_score", return_value=mock_collection),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.process_topic_collection_no_score", return_value=([], [])),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_prompt_keys", return_value=_mock_prompt_keys()),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_iterative_theme_user_prompt", return_value="prompt"),
        ):
            result = produce_bullets_for_theme(self._base_state(), make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_BULLETS_GENERATION

    def test_llm_exception_propagates(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("LLM error")

        with (
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.create_sources_for_results", return_value=(MagicMock(), {})),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_prompt_keys", return_value=_mock_prompt_keys()),
            patch("bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets.get_iterative_theme_user_prompt", return_value="prompt"),
        ):
            with pytest.raises(RuntimeError, match="LLM error"):
                produce_bullets_for_theme(self._base_state(), make_config(deps))


# ══════════════════════════════════════════════════════════════════════════════
# score_and_gate_bullet_relevance (relevance_score)
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreAndGateBulletRelevance:
    def _state_with_bullet(self, theme="Theme A", scored=False):
        bp = make_bullet(theme=theme)
        if scored:
            bp["relevance_scoring"] = {"score": 5, "reason": "good", "passed": True}
        return _state(
            themes=["Theme A"],
            active_theme_index=0,
            bullet_points=[bp],
        )

    def test_advances_active_theme_index(self):
        deps = make_deps()
        mock_result = MagicMock()
        mock_result.relevance_score = 5
        mock_result.reason = "relevant"
        deps.llm_client.call_with_response_format.return_value = mock_result

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
            result = score_and_gate_bullet_relevance(self._state_with_bullet(), make_config(deps))

        assert result["active_theme_index"] == 1

    def test_bullet_above_threshold_stays_active(self):
        deps = make_deps()
        mock_result = MagicMock()
        mock_result.relevance_score = 5
        mock_result.reason = "relevant"
        deps.llm_client.call_with_response_format.return_value = mock_result

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
            result = score_and_gate_bullet_relevance(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is True
        assert bp["relevance_scoring"]["score"] == 5

    def test_bullet_below_threshold_deactivated(self):
        deps = make_deps()
        mock_result = MagicMock()
        mock_result.relevance_score = 1  # below threshold (typically 2)
        mock_result.reason = "not relevant"
        deps.llm_client.call_with_response_format.return_value = mock_result

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.settings") as mock_settings:
            mock_settings.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
                result = score_and_gate_bullet_relevance(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False

    def test_already_scored_bullets_skipped(self):
        deps = make_deps()
        state = self._state_with_bullet(scored=True)

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
            result = score_and_gate_bullet_relevance(state, make_config(deps))

        deps.llm_client.call_with_response_format.assert_not_called()

    def test_llm_failure_marks_bullet_with_failure_record(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("timeout")

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
            result = score_and_gate_bullet_relevance(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["failure"] is not None
        assert bp["failure"]["node_id"] == NODE_RELEVANCE_SCORE
        assert bp["failure"]["error_type"] == "RuntimeError"
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_returns_node_metrics(self):
        deps = make_deps()
        mock_result = MagicMock(relevance_score=5, reason="ok")
        deps.llm_client.call_with_response_format.return_value = mock_result

        with patch("bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance.get_prompt_keys", return_value=_mock_prompt_keys()):
            result = score_and_gate_bullet_relevance(self._state_with_bullet(), make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_RELEVANCE_SCORE

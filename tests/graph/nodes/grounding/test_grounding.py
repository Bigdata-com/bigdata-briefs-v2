"""Tests for entity grounding nodes: validate + rewrite."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_ENTITY_GROUNDING_CHECK,
    NODE_REWRITE_ENTITY_GROUNDING,
)
from bigdata_briefs.graph.nodes.grounding.apply_grounding_rewrites import (
    apply_grounding_corrections,
)
from bigdata_briefs.graph.nodes.grounding.validate_entity_grounding import (
    classify_grounding_validity,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


def _mock_prompt():
    pk = MagicMock()
    pk.system_prompt = "system"
    pk.user_template.render.return_value = "user"
    pk.llm_kwargs = {}
    return pk


# ══════════════════════════════════════════════════════════════════════════════
# classify_grounding_validity (entity_grounding_check)
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyGroundingValidity:
    def _state_with_bullet(self, decision="valid"):
        bp = make_bullet(citations=["CQS:REF0"])
        return _state(
            bullet_points=[bp],
            source_references={"CQS:REF0": {"text": "Ref text", "headline": "H1"}},
        )

    def _grounding_result(self, decision, reason="", invalid_refs=None):
        r = MagicMock()
        r.decision = decision
        r.reason = reason
        r.invalid_references = invalid_refs or []
        return r

    def test_skips_when_no_active_bullets(self):
        state = _state(bullet_points=[], source_references={"r": {"text": "t"}})
        result = classify_grounding_validity(state, make_config())
        assert "bullet_points" not in result
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_valid_decision_keeps_bullet_active(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = self._grounding_result("VALID")

        with patch("bigdata_briefs.graph.nodes.grounding.validate_entity_grounding.get_prompt_keys", return_value=_mock_prompt()):
            result = classify_grounding_validity(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is True
        assert bp["entity_grounding"]["check"]["decision"] == "valid"

    def test_invalid_decision_deactivates_bullet(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = self._grounding_result("INVALID")

        with patch("bigdata_briefs.graph.nodes.grounding.validate_entity_grounding.get_prompt_keys", return_value=_mock_prompt()):
            result = classify_grounding_validity(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False

    def test_rewrite_decision_keeps_bullet_active_and_flags(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = self._grounding_result(
            "REWRITE", invalid_refs=["CQS:REF0"]
        )

        with patch("bigdata_briefs.graph.nodes.grounding.validate_entity_grounding.get_prompt_keys", return_value=_mock_prompt()):
            result = classify_grounding_validity(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is True
        assert bp["entity_grounding"]["check"]["decision"] == "rewrite"

    def test_llm_failure_marks_bullet_with_failure_record(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("LLM down")

        with patch("bigdata_briefs.graph.nodes.grounding.validate_entity_grounding.get_prompt_keys", return_value=_mock_prompt()):
            result = classify_grounding_validity(self._state_with_bullet(), make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["failure"]["node_id"] == NODE_ENTITY_GROUNDING_CHECK
        assert bp["failure"]["error_type"] == "RuntimeError"
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = self._grounding_result("VALID")

        with patch("bigdata_briefs.graph.nodes.grounding.validate_entity_grounding.get_prompt_keys", return_value=_mock_prompt()):
            result = classify_grounding_validity(self._state_with_bullet(), make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_ENTITY_GROUNDING_CHECK


# ══════════════════════════════════════════════════════════════════════════════
# apply_grounding_corrections (rewrite_entity_grounding)
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyGroundingCorrections:
    def _bullet_flagged_for_rewrite(self, invalid_refs=None):
        bp = make_bullet(text="Original text.", citations=["CQS:REF0", "CQS:REF1"])
        bp["entity_grounding"] = {
            "check": {
                "decision": "rewrite",
                "reason": "bad ref",
                "invalid_references": invalid_refs or ["CQS:REF1"],
            }
        }
        return bp

    def test_skips_when_no_rewrite_bullets(self):
        bp = make_bullet()  # no entity_grounding block
        state = _state(bullet_points=[bp])
        result = apply_grounding_corrections(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_rewrites_text_and_updates_citations(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = "Rewritten text."

        with patch("bigdata_briefs.graph.nodes.grounding.apply_grounding_rewrites.get_prompt_keys", return_value=_mock_prompt()):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()], entity_name="Test Corp")
            result = apply_grounding_corrections(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["text"] == "Rewritten text."
        # Invalid ref removed
        assert "CQS:REF1" not in bp["citations"]
        assert bp["entity_grounding"]["rewrite"]["text_before"] == "Original text."
        assert bp["entity_grounding"]["rewrite"]["text_after"] == "Rewritten text."

    def test_no_valid_refs_discards_bullet(self):
        deps = make_deps()
        # All citations are invalid
        bp = self._bullet_flagged_for_rewrite(invalid_refs=["CQS:REF0", "CQS:REF1"])
        state = _state(bullet_points=[bp])

        with patch("bigdata_briefs.graph.nodes.grounding.apply_grounding_rewrites.get_prompt_keys", return_value=_mock_prompt()):
            result = apply_grounding_corrections(state, make_config(deps))

        bp_out = result["bullet_points"][0]
        assert bp_out["is_active"] is False
        # LLM should not be called when no valid refs remain
        deps.llm_client.call_with_response_format.assert_not_called()

    def test_llm_failure_marks_bullet_with_failure_record(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.side_effect = RuntimeError("timeout")

        with patch("bigdata_briefs.graph.nodes.grounding.apply_grounding_rewrites.get_prompt_keys", return_value=_mock_prompt()):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()], entity_name="Test Corp")
            result = apply_grounding_corrections(state, make_config(deps))

        bp = result["bullet_points"][0]
        assert bp["is_active"] is False
        assert bp["failure"]["node_id"] == NODE_REWRITE_ENTITY_GROUNDING
        assert bp["failure"]["error_type"] == "RuntimeError"
        assert result["node_metrics"][0]["extra"]["failed_bullets"] == 1

    def test_returns_node_metrics(self):
        deps = make_deps()
        deps.llm_client.call_with_response_format.return_value = "Rewritten."

        with patch("bigdata_briefs.graph.nodes.grounding.apply_grounding_rewrites.get_prompt_keys", return_value=_mock_prompt()):
            state = _state(bullet_points=[self._bullet_flagged_for_rewrite()], entity_name="Test Corp")
            result = apply_grounding_corrections(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_REWRITE_ENTITY_GROUNDING

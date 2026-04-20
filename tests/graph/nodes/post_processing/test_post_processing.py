"""Tests for post-processing nodes: redundancy_check, thematic_consolidation, standalone_validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_REDUNDANCY_CHECK,
    NODE_STANDALONE_VALIDATION,
    NODE_THEMATIC_CONSOLIDATION,
)
from bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy import (
    detect_and_merge_redundant_bullets,
)
from bigdata_briefs.graph.nodes.post_processing.consolidate_themes import (
    cluster_and_consolidate_by_theme,
)
from bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets import (
    evaluate_standalone_bullet_actions,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


# ══════════════════════════════════════════════════════════════════════════════
# detect_and_merge_redundant_bullets (redundancy_check)
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectAndMergeRedundantBullets:
    def test_skips_when_processing_phase_disabled(self):
        state = _state(bullet_points=[make_bullet(), make_bullet()])
        with patch("bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = False
            result = detect_and_merge_redundant_bullets(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_fewer_than_two_active_bullets(self):
        state = _state(bullet_points=[make_bullet()])
        with patch("bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = detect_and_merge_redundant_bullets(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_unchanged_bullets_reactivated(self):
        deps = make_deps()
        bp1 = make_bullet(text="Revenue grew 10%.")
        bp2 = make_bullet(text="Margins improved.")
        # Service returns both texts unchanged (no redundancy)
        entity_report_mock = MagicMock()
        entity_report_mock.report_bulletpoints = ["Revenue grew 10%.", "Margins improved."]
        entity_report_mock.bullet_citations = [bp1["citations"], bp2["citations"]]
        deps.brief_service._apply_validation_bullet_redundancy.return_value = (entity_report_mock, 2)

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = detect_and_merge_redundant_bullets(state, make_config(deps))

        active = [bp for bp in result["bullet_points"] if bp["is_active"]]
        assert len(active) == 2

    def test_merged_bullet_creates_new_record(self):
        deps = make_deps()
        bp1 = make_bullet(text="Revenue grew 10%.")
        bp2 = make_bullet(text="Revenue increased by 10%.")
        # Service returns one merged text
        entity_report_mock = MagicMock()
        entity_report_mock.report_bulletpoints = ["Revenue grew about 10%."]
        entity_report_mock.bullet_citations = [["CQS:REF0"]]
        deps.brief_service._apply_validation_bullet_redundancy.return_value = (entity_report_mock, 2)

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = detect_and_merge_redundant_bullets(state, make_config(deps))

        active = [bp for bp in result["bullet_points"] if bp["is_active"]]
        assert len(active) == 1
        assert active[0]["text"] == "Revenue grew about 10%."

    def test_returns_node_metrics(self):
        deps = make_deps()
        bp1 = make_bullet(text="A.")
        bp2 = make_bullet(text="B.")
        entity_report_mock = MagicMock()
        entity_report_mock.report_bulletpoints = ["A.", "B."]
        entity_report_mock.bullet_citations = [bp1["citations"], bp2["citations"]]
        deps.brief_service._apply_validation_bullet_redundancy.return_value = (entity_report_mock, 2)

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = detect_and_merge_redundant_bullets(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_REDUNDANCY_CHECK


# ══════════════════════════════════════════════════════════════════════════════
# cluster_and_consolidate_by_theme (thematic_consolidation)
# ══════════════════════════════════════════════════════════════════════════════

class TestClusterAndConsolidateByTheme:
    def test_skips_when_processing_phase_disabled(self):
        state = _state(bullet_points=[make_bullet(), make_bullet()])
        with patch("bigdata_briefs.graph.nodes.post_processing.consolidate_themes.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = False
            result = cluster_and_consolidate_by_theme(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_fewer_than_two_active_bullets(self):
        state = _state(bullet_points=[make_bullet()])
        with patch("bigdata_briefs.graph.nodes.post_processing.consolidate_themes.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = cluster_and_consolidate_by_theme(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_standalone_bullets_tagged_with_standalone_theme(self):
        deps = make_deps()
        bp1 = make_bullet(text="Revenue grew 10%.", theme="Revenue")
        bp2 = make_bullet(text="Standalone fact.", theme="Other")
        # Service returns bp1 as consolidated, bp2 as standalone
        deps.brief_service._consolidate_bullets.return_value = (
            ["Revenue grew 10%."],       # consolidated_bullets
            [bp1["citations"]],           # consolidated_cits
            [5],                          # consolidated_scores
            ["Standalone fact."],         # standalone_bullets
            [bp2["citations"]],           # standalone_cits
            [4],                          # standalone_scores
        )

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.consolidate_themes.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = cluster_and_consolidate_by_theme(state, make_config(deps))

        standalone = [bp for bp in result["bullet_points"] if bp.get("theme") == "__standalone__"]
        assert len(standalone) == 1
        assert standalone[0]["text"] == "Standalone fact."

    def test_consolidated_bullets_are_active(self):
        deps = make_deps()
        bp1 = make_bullet(text="Revenue grew 10%.", theme="Revenue")
        bp2 = make_bullet(text="Margins improved.", theme="Revenue")
        deps.brief_service._consolidate_bullets.return_value = (
            ["Revenue and margins both improved."],
            [["CQS:REF0"]],
            [5],
            [],
            [],
            [],
        )

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.consolidate_themes.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = cluster_and_consolidate_by_theme(state, make_config(deps))

        active = [bp for bp in result["bullet_points"] if bp["is_active"]]
        assert len(active) == 1
        assert active[0]["text"] == "Revenue and margins both improved."

    def test_returns_node_metrics(self):
        deps = make_deps()
        bp1 = make_bullet(text="A.", theme="T")
        bp2 = make_bullet(text="B.", theme="T")
        deps.brief_service._consolidate_bullets.return_value = (["A."], [bp1["citations"]], [5], [], [], [])

        state = _state(bullet_points=[bp1, bp2])
        with patch("bigdata_briefs.graph.nodes.post_processing.consolidate_themes.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = cluster_and_consolidate_by_theme(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_THEMATIC_CONSOLIDATION


# ══════════════════════════════════════════════════════════════════════════════
# evaluate_standalone_bullet_actions (standalone_validation)
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateStandaloneBulletActions:
    def _bullet_standalone(self, text="Standalone fact."):
        bp = make_bullet(text=text)
        bp["theme"] = "__standalone__"
        return bp

    def test_skips_when_processing_phase_disabled(self):
        state = _state(bullet_points=[self._bullet_standalone()])
        with patch("bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = False
            result = evaluate_standalone_bullet_actions(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_skips_when_no_standalone_bullets(self):
        state = _state(bullet_points=[make_bullet()])  # no __standalone__ theme
        with patch("bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = evaluate_standalone_bullet_actions(state, make_config())
        assert result["node_metrics"][0]["extra"]["skipped"] is True

    def test_kept_standalone_clears_standalone_tag(self):
        deps = make_deps()
        consolidated = make_bullet(text="Revenue grew.", theme="Revenue")
        standalone = self._bullet_standalone("New standalone fact.")

        # Service keeps standalone bullet with text match
        deps.brief_service._validate_standalone_bullets.return_value = (
            ["Revenue grew.", "New standalone fact."],
            [consolidated["citations"], standalone["citations"]],
            [5, 4],
        )

        state = _state(bullet_points=[consolidated, standalone])
        with patch("bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = evaluate_standalone_bullet_actions(state, make_config(deps))

        active = [bp for bp in result["bullet_points"] if bp["is_active"]]
        assert len(active) == 2
        # Standalone tag should be cleared on the previously-standalone bullet
        standalone_tag_bullets = [bp for bp in active if bp.get("theme") == "__standalone__"]
        assert len(standalone_tag_bullets) == 0

    def test_discarded_standalone_becomes_inactive(self):
        deps = make_deps()
        consolidated = make_bullet(text="Revenue grew.", theme="Revenue")
        standalone = self._bullet_standalone("Irrelevant fact.")

        # Service only returns consolidated bullet (standalone was discarded)
        deps.brief_service._validate_standalone_bullets.return_value = (
            ["Revenue grew."],
            [consolidated["citations"]],
            [5],
        )

        state = _state(bullet_points=[consolidated, standalone])
        with patch("bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = evaluate_standalone_bullet_actions(state, make_config(deps))

        active = [bp for bp in result["bullet_points"] if bp["is_active"]]
        assert len(active) == 1
        assert active[0]["text"] == "Revenue grew."

    def test_returns_node_metrics(self):
        deps = make_deps()
        consolidated = make_bullet(text="A.", theme="T")
        standalone = self._bullet_standalone("B.")
        deps.brief_service._validate_standalone_bullets.return_value = (["A.", "B."], [["r0"], ["r1"]], [5, 4])

        state = _state(bullet_points=[consolidated, standalone])
        with patch("bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets.settings") as ms:
            ms.ENABLE_BULLET_PROCESSING_PHASE = True
            ms.INTRO_SECTION_MIN_RELEVANCE_SCORE = 2
            result = evaluate_standalone_bullet_actions(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_STANDALONE_VALIDATION

"""Tests for save_novel_bullets node."""

from __future__ import annotations

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import (
    NODE_SAVE_NOVEL_BULLETS,
    PIPELINE_STATUS_NO_DATA,
    PIPELINE_STATUS_RUNNING,
)
from bigdata_briefs.graph.nodes.reconcile.save_novel_bullets import (
    save_novel_bullet_points,
)
from bigdata_briefs.graph.state import (
    EmbeddingJudgmentMetadata,
    NoveltyEmbeddingBlock,
    NoveltySearchBlock,
    SearchNoveltyMetadata,
    bullet_to_record,
    record_to_bullet,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


class TestSaveNovelBulletPoints:
    def test_active_bullets_set_running_status(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        state = _state(bullet_points=[bp])
        result = save_novel_bullet_points(state, make_config(deps))

        assert result["pipeline_status"] == PIPELINE_STATUS_RUNNING

    def test_all_inactive_bullets_set_no_data_status(self):
        deps = make_deps()
        bp = make_bullet(is_active=False)
        state = _state(bullet_points=[bp])
        result = save_novel_bullet_points(state, make_config(deps))

        assert result["pipeline_status"] == PIPELINE_STATUS_NO_DATA

    def test_empty_bullet_list_sets_no_data_status(self):
        deps = make_deps()
        state = _state(bullet_points=[])
        result = save_novel_bullet_points(state, make_config(deps))

        assert result["pipeline_status"] == PIPELINE_STATUS_NO_DATA

    def test_returns_node_metrics(self):
        deps = make_deps()
        state = _state(bullet_points=[make_bullet()])
        result = save_novel_bullet_points(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_SAVE_NOVEL_BULLETS

    def test_storage_called_with_only_active_bullets(self):
        deps = make_deps()
        active = make_bullet(is_active=True, text="Active bullet")
        inactive = make_bullet(is_active=False, text="Inactive bullet")
        state = _state(bullet_points=[active, inactive])
        save_novel_bullet_points(state, make_config(deps))

        deps.generated_bullet_storage.store.assert_called_once()
        stored = deps.generated_bullet_storage.store.call_args[0][0]
        assert len(stored) == 1
        assert stored[0].text == "Active bullet"

    def test_no_active_bullets_still_calls_store_with_empty_list(self):
        deps = make_deps()
        state = _state(bullet_points=[make_bullet(is_active=False)])
        save_novel_bullet_points(state, make_config(deps))

        deps.generated_bullet_storage.store.assert_called_once()
        stored = deps.generated_bullet_storage.store.call_args[0][0]
        assert stored == []

    def test_metrics_include_saved_count(self):
        deps = make_deps()
        bp1 = make_bullet(is_active=True)
        bp2 = make_bullet(is_active=True)
        bp3 = make_bullet(is_active=False)

        state = _state(bullet_points=[bp1, bp2, bp3])
        result = save_novel_bullet_points(state, make_config(deps))

        extra = result["node_metrics"][0]["extra"]
        assert extra["saved_bullets"] == 2
        assert extra["pipeline_status"] == PIPELINE_STATUS_RUNNING

    def test_extracts_embedding_and_search_decisions(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        rec = bullet_to_record(bp)
        rec.novelty_embedding = NoveltyEmbeddingBlock(
            judgment=EmbeddingJudgmentMetadata(
                decision="rewrite", reason="partial", evaluator_details=[]
            )
        )
        rec.novelty_search = NoveltySearchBlock(
            search=SearchNoveltyMetadata(verdict="keep", rewritten_text=None, duration_seconds=0.1)
        )
        bp = record_to_bullet(rec)

        state = _state(bullet_points=[bp])
        save_novel_bullet_points(state, make_config(deps))

        stored = deps.generated_bullet_storage.store.call_args[0][0]
        assert stored[0].embedding_decision == "rewrite"
        assert stored[0].search_action == "keep"

    def test_decisions_are_none_when_no_novelty_metadata(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        state = _state(bullet_points=[bp])
        save_novel_bullet_points(state, make_config(deps))

        stored = deps.generated_bullet_storage.store.call_args[0][0]
        assert stored[0].embedding_decision is None
        assert stored[0].search_action is None

    def test_run_id_is_request_id_from_state(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        state = _state(bullet_points=[bp], request_id="req-XYZ")
        save_novel_bullet_points(state, make_config(deps))

        stored = deps.generated_bullet_storage.store.call_args[0][0]
        assert stored[0].run_id == "req-XYZ"

    def test_citations_resolved_from_source_references(self):
        # source_references uses CQS:REF{n} keys; citations use CQS:{doc_id}-{chunk_id}.
        # The node must build a reverse lookup to match them.
        deps = make_deps()
        bp = make_bullet(is_active=True, citations=["CQS:DOCAAA-1", "CQS:DOCBBB-2"])
        source_refs = {
            "CQS:REF0": {
                "document_id": "DOCAAA", "chunk_id": 1,
                "headline": "Apple Reports Q3 Results",
                "text": "Apple revenue grew 8% YoY.",
                "source_name": "Benzinga",
            },
            "CQS:REF1": {
                "document_id": "DOCBBB", "chunk_id": 2,
                "headline": "Services segment analysis",
                "text": "Services margin reached 74%.",
                "source_name": "Yahoo! Finance",
            },
        }
        state = _state(bullet_points=[bp], source_references=source_refs)
        save_novel_bullet_points(state, make_config(deps))

        stored = deps.generated_bullet_storage.store.call_args[0][0]
        citations = stored[0].citations
        assert len(citations) == 2
        assert citations[0].id == "CQS:DOCAAA-1"
        assert citations[0].headline == "Apple Reports Q3 Results"
        assert citations[0].text == "Apple revenue grew 8% YoY."
        assert citations[0].source_name == "Benzinga"
        assert citations[1].id == "CQS:DOCBBB-2"
        assert citations[1].headline == "Services segment analysis"
        assert citations[1].source_name == "Yahoo! Finance"

    def test_citation_id_not_in_source_references_preserved_with_empty_fields(self):
        """IDs missing from source_references are kept with empty headline/text
        so no data is silently lost. This should not occur in normal operation."""
        deps = make_deps()
        bp = make_bullet(is_active=True, citations=["CQS:DOCAAA-1", "CQS:MISSING-9"])
        source_refs = {
            "CQS:REF0": {
                "document_id": "DOCAAA", "chunk_id": 1,
                "headline": "Known Source", "text": "Known text.",
                "source_name": "PubT",
            },
        }
        state = _state(bullet_points=[bp], source_references=source_refs)
        save_novel_bullet_points(state, make_config(deps))

        stored = deps.generated_bullet_storage.store.call_args[0][0]
        citations = stored[0].citations
        assert len(citations) == 2
        assert citations[0].id == "CQS:DOCAAA-1"
        assert citations[0].headline == "Known Source"
        assert citations[0].source_name == "PubT"
        assert citations[1].id == "CQS:MISSING-9"
        assert citations[1].headline == ""
        assert citations[1].text == ""
        assert citations[1].source_name == ""

"""Tests for persist_novel_embeddings node."""

from __future__ import annotations

import pytest

from tests.graph.conftest import BASE_STATE, make_bullet, make_config, make_deps

from bigdata_briefs.graph.constants import NODE_PERSIST_NOVEL_EMBEDDINGS
from bigdata_briefs.graph.nodes.novelty_embedding.persist_novel_embeddings import (
    persist_embeddings_of_novel_bullets,
)


def _state(**overrides):
    return {**BASE_STATE, **overrides}


class TestPersistEmbeddingsOfNovelBullets:
    def test_active_bullet_with_vector_is_persisted(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        deps._embedding_cache[bp["trace_id"]] = [0.1, 0.2, 0.3]

        state = _state(bullet_points=[bp])
        persist_embeddings_of_novel_bullets(state, make_config(deps))

        deps.novelty_service._store_embedding.assert_called_once()
        call_kwargs = deps.novelty_service._store_embedding.call_args.kwargs
        assert len(call_kwargs["embedding_bp"]) == 1
        assert call_kwargs["embedding_bp"][0].status == "keep"
        assert call_kwargs["embedding_bp"][0].novelty is True

    def test_inactive_bullet_is_skipped(self):
        deps = make_deps()
        bp = make_bullet(is_active=False)
        deps._embedding_cache[bp["trace_id"]] = [0.1, 0.2]

        state = _state(bullet_points=[bp])
        persist_embeddings_of_novel_bullets(state, make_config(deps))

        deps.novelty_service._store_embedding.assert_not_called()

    def test_bullet_without_cached_vector_is_skipped(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        # no cache entry
        state = _state(bullet_points=[bp])
        persist_embeddings_of_novel_bullets(state, make_config(deps))

        deps.novelty_service._store_embedding.assert_not_called()

    def test_cache_cleared_after_persist(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        deps._embedding_cache[bp["trace_id"]] = [0.1, 0.2]

        state = _state(bullet_points=[bp])
        persist_embeddings_of_novel_bullets(state, make_config(deps))

        assert deps._embedding_cache == {}

    def test_cache_cleared_even_on_store_failure(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        deps._embedding_cache[bp["trace_id"]] = [0.1, 0.2]
        deps.novelty_service._store_embedding.side_effect = RuntimeError("DB down")

        state = _state(bullet_points=[bp])
        with pytest.raises(RuntimeError, match="DB down"):
            persist_embeddings_of_novel_bullets(state, make_config(deps))

        assert deps._embedding_cache == {}

    def test_returns_node_metrics(self):
        deps = make_deps()
        state = _state(bullet_points=[])
        result = persist_embeddings_of_novel_bullets(state, make_config(deps))

        assert result["node_metrics"][0]["node_id"] == NODE_PERSIST_NOVEL_EMBEDDINGS

    def test_metrics_count_persisted_and_skipped(self):
        deps = make_deps()
        persisted_bp = make_bullet(is_active=True)
        inactive_bp = make_bullet(is_active=False)
        no_vec_bp = make_bullet(is_active=True)
        deps._embedding_cache[persisted_bp["trace_id"]] = [0.1, 0.2]

        state = _state(bullet_points=[persisted_bp, inactive_bp, no_vec_bp])
        result = persist_embeddings_of_novel_bullets(state, make_config(deps))

        extra = result["node_metrics"][0]["extra"]
        assert extra["persisted"] == 1
        assert "skipped_inactive" not in extra
        assert extra["skipped_no_vector"] == 1

    def test_does_not_modify_bullet_points(self):
        deps = make_deps()
        bp = make_bullet(is_active=True)
        deps._embedding_cache[bp["trace_id"]] = [0.1]

        state = _state(bullet_points=[bp])
        result = persist_embeddings_of_novel_bullets(state, make_config(deps))

        assert "bullet_points" not in result

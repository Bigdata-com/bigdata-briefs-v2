"""
RuntimeDependencies: container for all injected services used by graph nodes.

Passed to every node via LangGraph's RunnableConfig:

    from langchain_core.runnables import RunnableConfig

    def some_node(state: BriefGraphState, config: RunnableConfig) -> dict:
        deps: RuntimeDependencies = config["configurable"]["deps"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from bigdata_briefs.query_service.base import BaseQueryService
    from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
    from bigdata_briefs.llm_client import LLMClient
    from bigdata_briefs.service import BriefPipelineService
    from bigdata_briefs.novelty.novelty_service import NoveltyFilteringService
    from bigdata_briefs.novelty.embedding_client import EmbeddingClient
    from bigdata_briefs.novelty.embedding_storage import EmbeddingStorage
    from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
    from bigdata_briefs.debug_logger import DebugLogger
    from bigdata_briefs.metrics import EntityStepMetrics


@dataclass
class RuntimeDependencies:
    """
    Holds all service dependencies injected into graph nodes at runtime.

    ``engine`` is used by the ``initialize_pipeline`` node to create the DB
    schema on first run (idempotent via ``ensure_orchestration_schema``).

    Embedding vectors from ``embed_and_retrieve`` are too large to store in
    LangGraph state (they bloat checkpoints). They are cached here keyed by
    ``trace_id`` and cleared after ``persist_novel_embeddings``.
    """

    engine: "Engine"
    query_service: "BaseQueryService"
    llm_client: "LLMClient"
    brief_service: "BriefPipelineService"
    novelty_service: "NoveltyFilteringService"
    embedding_client: "EmbeddingClient"
    embedding_storage: "EmbeddingStorage"
    generated_bullet_storage: "SQLiteGeneratedBulletPointStorage"
    debug_logger: "DebugLogger | None" = None
    entity_metrics: "EntityStepMetrics | None" = None
    # Shared Bigdata QPM limiter. When None (CLI / legacy callers), rate-limiting
    # is only enforced inside APIQueryService. When set (FastAPI lifespan), graph
    # nodes with their own HTTP paths (e.g. novelty_search) can route calls
    # through ``bigdata_rate_limiter.acquire()`` / ``aacquire()`` to share the
    # one process-global 450 QPM budget.
    bigdata_rate_limiter: "RequestsPerMinuteController | None" = None

    # Transient cache: trace_id -> embedding vector (list[float])
    # Populated by embed_and_retrieve, consumed by novelty_judgment_embedding,
    # cleared by persist_novel_embeddings.
    _embedding_cache: dict[str, list[float]] = field(default_factory=dict)

    def store_embedding(self, trace_id: str, vector: list[float]) -> None:
        """Cache an embedding vector for a bullet by its trace_id."""
        self._embedding_cache[trace_id] = vector

    def get_embedding(self, trace_id: str) -> list[float] | None:
        """Retrieve a cached embedding vector, or None if not found."""
        return self._embedding_cache.get(trace_id)

    def clear_embedding_cache(self) -> None:
        """Release all cached vectors (called by persist_novel_embeddings)."""
        self._embedding_cache.clear()

    # Transient cache for novelty search intermediate data (keyed by trace_id)
    # Populated per node: parse -> fetch -> judgment -> cleared by rewrite
    _search_cache: dict[str, dict] = field(default_factory=dict)

    def store_search_data(self, trace_id: str, key: str, value) -> None:
        """Store one intermediate value in the search cache for a bullet."""
        if trace_id not in self._search_cache:
            self._search_cache[trace_id] = {}
        self._search_cache[trace_id][key] = value

    def get_search_data(self, trace_id: str, key: str):
        """Retrieve one intermediate value from the search cache."""
        return self._search_cache.get(trace_id, {}).get(key)

    def get_search_cache_entry(self, trace_id: str) -> dict:
        """Return the full cache dict for a bullet (empty dict if not found)."""
        return self._search_cache.get(trace_id, {})

    def clear_search_cache(self) -> None:
        """Release all cached search data (called by rewrite_search_bullets)."""
        self._search_cache.clear()


def get_deps(config: dict) -> RuntimeDependencies:
    """Extract RuntimeDependencies from a LangGraph RunnableConfig dict."""
    return config["configurable"]["deps"]

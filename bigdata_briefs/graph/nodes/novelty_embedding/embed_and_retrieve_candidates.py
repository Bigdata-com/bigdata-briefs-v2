"""
Node: embed_and_retrieve

Compute embedding vectors for all active bullets and cache them in
``deps._embedding_cache`` (keyed by ``trace_id``).

These cached vectors serve two purposes:
  1. Pre-computed embeddings available for the novelty judgment node.
  2. Vectors to be persisted to embedding storage by ``persist_novel_embeddings``.

Nothing is written to LangGraph state (embedding vectors are too large for
checkpoints). Only ``node_metrics`` is returned.

Service type: embed (single embedding API batch call)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_EMBED_AND_RETRIEVE,
    SERVICE_TYPE_EMBED,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.models import SingleEntityReport


def compute_embeddings_and_retrieve_candidates(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — embed_and_retrieve.

    Computes embedding vectors for every active bullet point and caches them in
    ``deps._embedding_cache``.  The cache is keyed by ``trace_id`` so downstream
    nodes can look up vectors by bullet identity.

    Calls ``deps.novelty_service._compute_embeddings()`` as a single batched
    embedding API request (one call for all active bullets).
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_name: str = state["entity_name"]
    bullet_points: list[dict] = state.get("bullet_points") or []

    # Collect active bullets and their texts
    active_pairs: list[tuple[int, str, str]] = []  # (original_idx, trace_id, text)
    for idx, bp in enumerate(bullet_points):
        if not bp.get("is_active", True):
            continue
        record = bullet_to_record(bp)
        text = record.text or ""
        if text:
            active_pairs.append((idx, record.trace_id, text))

    if not active_pairs:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_EMBED_AND_RETRIEVE,
                    service_type=SERVICE_TYPE_EMBED,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets"},
                ).model_dump()
            ]
        }

    texts = [t for _, _, t in active_pairs]

    # Normalize texts with entity prefix (mirrors what novelty_embedding_step does)
    clean_texts = [
        deps.novelty_service._normalize_text_for_embedding(
            SingleEntityReport.remove_references(t) if hasattr(SingleEntityReport, "remove_references") else t,
            entity_name,
        )
        for t in texts
    ]

    embeddings: list[list[float]] = deps.novelty_service.embedding_client.compute(
        clean_texts, entity_metrics=deps.entity_metrics
    )

    # Cache by trace_id
    for (_idx, trace_id, _text), vector in zip(active_pairs, embeddings):
        deps.store_embedding(trace_id, vector)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_EMBED_AND_RETRIEVE,
        service_type=SERVICE_TYPE_EMBED,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "bullets_embedded": len(active_pairs),
        },
    )

    return {"node_metrics": [metrics.model_dump()]}

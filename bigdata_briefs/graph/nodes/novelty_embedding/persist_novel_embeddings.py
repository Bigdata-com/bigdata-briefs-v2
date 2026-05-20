"""
Node: persist_novel_embeddings

Runs right after `relevance_check_embedding`.  Persists the embedding vector
for every bullet that survived the embedding-novelty phase (i.e. `is_active=True`
at this point) to the `sqlbulletpointembedding` table.

Why save here (instead of at the end of the pipeline)?
  - Bullets that pass the embedding novelty check but are later discarded by
    the search-novelty phase should still live in the embedding archive.
  - Future runs that produce similar bullets will then be caught at the
    (cheaper) embedding check and will not need to run the search check again.

Saved fields: entity_id, date, embedding, original_text, status="keep",
novelty=True, status_embedding=True, report_window_*, earnings_call_date.

The in-memory embedding cache is cleared after the write.

Service type: none (DB write, no LLM or search API)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_PERSIST_NOVEL_EMBEDDINGS,
    SERVICE_TYPE_NONE,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.novelty.models import BulletPointEmbedding


def persist_embeddings_of_novel_bullets(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — persist_novel_embeddings.

    Writes the cached embedding vectors for all still-active bullets to the
    embedding storage and clears the in-memory cache.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_id: str = state["entity_id"]
    current_quarter_title: str | None = state.get("current_quarter_title") or None

    end_date = datetime.fromisoformat(state["report_end_date"])
    start_date = datetime.fromisoformat(state["report_start_date"])
    save_emb_date = end_date

    bullet_points: list[dict] = state.get("bullet_points") or []

    to_store: list[BulletPointEmbedding] = []
    persisted = skipped_no_vector = 0

    for bp in bullet_points:
        record = bullet_to_record(bp)

        if not record.is_active:
            continue

        vector = deps.get_embedding(record.trace_id)
        if vector is None:
            skipped_no_vector += 1
            continue

        to_store.append(
            BulletPointEmbedding(
                date=save_emb_date,
                entity_id=entity_id,
                embedding=vector,
                original_text=record.text,
                status="keep",
                novelty=True,
                status_embedding=True,
                earnings_call_date=current_quarter_title,
                report_window_start=start_date,
                report_window_end=end_date,
            )
        )
        persisted += 1

    try:
        if to_store:
            deps.novelty_service._store_embedding(
                entity_id=entity_id,
                current_embedding_dt=save_emb_date,
                embedding_bp=to_store,
            )
    finally:
        deps.clear_embedding_cache()

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_PERSIST_NOVEL_EMBEDDINGS,
        service_type=SERVICE_TYPE_NONE,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "persisted": persisted,
            "skipped_no_vector": skipped_no_vector,
        },
    )

    return {"node_metrics": [metrics.model_dump()]}

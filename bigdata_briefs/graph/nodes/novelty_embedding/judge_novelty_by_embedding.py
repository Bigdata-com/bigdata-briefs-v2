"""
Node: novelty_judgment_embedding

Runs the Step-1 LLM novelty evaluation for every active bullet using
embedding-assisted retrieval (three parallel evaluators: novelty window,
remaining window, full history).

Decision per bullet:
  - KEEP    → ``novelty_embedding.judgment`` written; bullet unchanged
  - DISCARD → ``novelty_embedding.judgment`` written; ``is_active=False``
  - REWRITE → ``novelty_embedding.judgment`` written; bullet flagged for Step 2
               (handled by ``rewrite_non_novel_bullets``)

Calls ``NoveltyFilteringService.novelty_embedding_step`` with ``judge=None``
so that Step 2 rewrites are NOT performed here.  Keeps the rewrite and
relevance-check as separate LLM nodes.

The embeddings returned by ``novelty_embedding_step`` are written back into
``deps._embedding_cache`` (overwriting the pre-computed ones from
``embed_and_retrieve``) so that ``persist_novel_embeddings`` can persist the
canonical vectors.

Service type: llm (parallel LLM calls, one per evaluator per active bullet)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    DECISION_DISCARD,
    DECISION_REWRITE,
    NODE_NOVELTY_JUDGMENT_EMBEDDING,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    EmbeddingJudgmentMetadata,
    NoveltyEmbeddingBlock,
    NodeMetricsRecord,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.novelty.evaluators import make_three_window_evaluators
from bigdata_briefs.settings import settings


def evaluate_novelty_by_embedding_similarity(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — novelty_judgment_embedding.

    Evaluates the novelty of every active bullet using embedding similarity
    against historical bullets.  Step-1 only (no inline rewrite).

    Writes ``novelty_embedding.judgment`` on each processed bullet.
    DISCARD bullets are immediately deactivated.
    REWRITE bullets remain active and are handled by the next node.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_name: str = state["entity_name"]
    entity_id: str = state["entity_id"]
    entity_ticker: str | None = state.get("entity_ticker") or None
    current_quarter_title: str | None = state.get("current_quarter_title") or None

    start_date = datetime.fromisoformat(state["report_start_date"])
    end_date = datetime.fromisoformat(state["report_end_date"])
    # Novelty lookback window: [start_date - LOOKBACK, start_date]
    novelty_start = start_date - timedelta(days=settings.NOVELTY_LOOKBACK_DAYS)
    novelty_end = start_date
    current_date = end_date

    bullet_points: list[dict] = state.get("bullet_points") or []
    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    if not active_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_JUDGMENT_EMBEDDING,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets"},
                ).model_dump()
            ]
        }

    # Build ordered list of (index, trace_id, text) for active bullets
    active_entries: list[tuple[int, str, str]] = []
    for i in active_indices:
        record = bullet_to_record(bullet_points[i])
        active_entries.append((i, record.trace_id, record.text))

    texts = [t for _, _, t in active_entries]

    # Build evaluators (step 1 only — judge=None skips step 2 rewrite)
    evaluators, _judge = make_three_window_evaluators(
        deps.embedding_client,
        deps.embedding_storage,
        deps.llm_client,
        threshold=settings.NOVELTY_PREFILTER_THRESHOLD,
        top_k=settings.NOVELTY_PREFILTER_TOP_K,
    )

    current_datetime_str = (current_date - timedelta(seconds=1)).strftime("%A, %B %d, %Y")

    _kept, llm_results, all_embeddings, _ = deps.novelty_service.novelty_embedding_step(
        texts=texts,
        entity_id=entity_id,
        entity_name=entity_name,
        evaluators=evaluators,
        start_date=novelty_start,
        end_date=novelty_end,
        current_date=current_date,
        clean_up_func=None,
        current_quarter_title=current_quarter_title,
        debug_logger=deps.debug_logger,
        entity_metrics=deps.entity_metrics,
        judge=None,          # Step 2 handled by rewrite_non_novel_bullets node
        llm_client=None,     # No post-rewrite relevance here either
        entity_ticker=entity_ticker,
        current_datetime_str=current_datetime_str,
    )

    # Update embedding cache with canonical vectors from novelty_embedding_step
    for (_, trace_id, _text), vector in zip(active_entries, all_embeddings):
        deps.store_embedding(trace_id, vector)

    # Apply judgment results to bullet records
    updated = list(bullet_points)
    keep_count = discard_count = rewrite_count = 0

    for pos, (bullet_idx, _trace_id, _text) in enumerate(active_entries):
        result = llm_results[pos]
        record = bullet_to_record(updated[bullet_idx])

        # Normalize to lowercase at the LLM boundary so all downstream code uses
        # consistent lowercase values ("keep", "discard", "rewrite").
        normalized_decision = result.decision.lower()
        normalized_details = [
            {**d, "decision": d["decision"].lower()} if isinstance(d, dict) and "decision" in d else d
            for d in (result.evaluator_details or [])
        ]

        record.novelty_embedding = NoveltyEmbeddingBlock(
            judgment=EmbeddingJudgmentMetadata(
                decision=normalized_decision,
                reason=result.reason or "",
                evaluator_details=normalized_details,
            )
        )

        if normalized_decision == DECISION_DISCARD:
            record.is_active = False
            discard_count += 1
        elif normalized_decision == DECISION_REWRITE:
            rewrite_count += 1
        else:
            keep_count += 1

        updated[bullet_idx] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_NOVELTY_JUDGMENT_EMBEDDING,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=len(active_entries),
        extra={
            "keep": keep_count,
            "discard": discard_count,
            "rewrite": rewrite_count,
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

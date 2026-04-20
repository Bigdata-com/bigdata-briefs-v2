"""
Node: relevance_check_embedding

Final gate in the novelty-embedding phase.

For every bullet that was rewritten by ``rewrite_embedding`` (those with a
non-empty ``novelty_embedding.rewrite.text_after``), runs a relevance LLM
call to confirm the rewritten text is still relevant to the entity and
reporting period.

Bullets whose relevance score falls at or below
``INTRO_SECTION_MIN_RELEVANCE_SCORE`` are deactivated — the rewrite stripped
so much content that the result is no longer useful.

Service type: llm (parallel LLM calls, one per rewritten bullet)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_RELEVANCE_CHECK_EMBEDDING,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    EmbeddingRelevanceMetadata,
    NodeMetricsRecord,
    NoveltyEmbeddingBlock,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.novelty.novelty_service import _run_relevance_check_on_rewrite
from bigdata_briefs.settings import settings


def score_embedding_rewrite_relevance(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — relevance_check_embedding.

    Runs a relevance check on every bullet rewritten by the novelty-embedding
    step.  Bullets that do not meet the relevance threshold are deactivated.

    Writes ``novelty_embedding.relevance_check`` on each assessed bullet.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_name: str = state["entity_name"]
    entity_ticker: str | None = state.get("entity_ticker") or None
    current_quarter_title: str | None = state.get("current_quarter_title") or None

    end_date = datetime.fromisoformat(state["report_end_date"])
    current_date = end_date
    current_datetime_str = (current_date - timedelta(seconds=1)).strftime("%A, %B %d, %Y")

    threshold = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE
    default_score = threshold + 1

    bullet_points: list[dict] = state.get("bullet_points") or []

    # Find bullets that were rewritten (non-empty text_after) and are still active
    check_indices = [
        i for i, bp in enumerate(bullet_points)
        if bp.get("is_active", True)
        and ((bp.get("novelty_embedding") or {}).get("rewrite") or {}).get("text_after", "").strip()
    ]

    if not check_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_RELEVANCE_CHECK_EMBEDDING,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no rewritten bullets to check"},
                ).model_dump()
            ]
        }

    def check_single(bullet_idx: int) -> tuple[int, int]:
        bp = bullet_points[bullet_idx]
        rewritten_text: str = ((bp.get("novelty_embedding") or {}).get("rewrite") or {}).get("text_after", "")
        score = _run_relevance_check_on_rewrite(
            rewritten_text=rewritten_text,
            entity_name=entity_name,
            entity_ticker=entity_ticker,
            current_datetime_str=current_datetime_str,
            current_quarter_title=current_quarter_title,
            bullet_index=bullet_idx,
            llm_client=deps.llm_client,
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
        )
        return bullet_idx, score

    # Run relevance checks in parallel
    score_map: dict[int, int] = {}
    failures: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_single, i): i for i in check_indices}
        for future in as_completed(futures):
            bidx = futures[future]
            try:
                bidx, score = future.result()
                score_map[bidx] = score
            except Exception as e:
                failures[bidx] = e

    updated = list(bullet_points)
    passed_count = failed_count = 0

    for i in check_indices:
        record = bullet_to_record(updated[i])

        if i in failures:
            e = failures[i]
            record.is_active = False
            record.failure = BulletFailure(
                node_id=NODE_RELEVANCE_CHECK_EMBEDDING,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            updated[i] = record_to_bullet(record)
            continue

        score = score_map.get(i, default_score)
        passed = score > threshold

        if record.novelty_embedding is None:
            record.novelty_embedding = NoveltyEmbeddingBlock()

        record.novelty_embedding.relevance_check = EmbeddingRelevanceMetadata(
            score=score,
            passed=passed,
        )
        if not passed:
            record.is_active = False
            failed_count += 1
        else:
            passed_count += 1

        updated[i] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_RELEVANCE_CHECK_EMBEDDING,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=len(check_indices),
        extra={
            "passed": passed_count,
            "failed_relevance": failed_count,
            "failed_bullets": len(failures),
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

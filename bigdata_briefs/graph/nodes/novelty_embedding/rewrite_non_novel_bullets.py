"""
Node: rewrite_embedding

Step 2 of the novelty embedding pipeline.

For bullets that received a ``REWRITE`` decision in
``novelty_judgment_embedding``, this node calls ``LLMNoveltyJudge.run_step2_rewrite``
to produce a revised bullet that removes previously-reported facts while
preserving any genuinely novel content.

Outcomes per REWRITE bullet:
  - Step 2 returns a non-empty rewrite → ``text`` updated, ``novelty_embedding.rewrite``
    populated, bullet remains active
  - Step 2 returns empty (``is_empty=True``) → bullet set to ``is_active=False``,
    ``novelty_embedding.rewrite`` recorded with ``is_empty=True``

Service type: llm (parallel LLM calls, one per REWRITE-flagged bullet)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_REWRITE_EMBEDDING,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    EmbeddingRewriteMetadata,
    NodeMetricsRecord,
    NoveltyEmbeddingBlock,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.novelty.evaluators import LLMNoveltyJudge, NoveltyContext
from bigdata_briefs.settings import settings


def _extract_reviewer_notes(evaluator_details: list[dict]) -> list[str]:
    """Extract step-2 rewrite instructions from per-evaluator detail dicts.

    Mirrors ``_step2_notes_from_rewrite_evaluators`` in novelty_service but
    operates on the serialised dict form stored in ``EmbeddingJudgmentMetadata``.
    """
    notes: list[str] = []
    for detail in evaluator_details:
        if detail.get("decision") != "rewrite":
            continue
        inst = (detail.get("instruction") or "").strip()
        if inst:
            notes.append(inst)
        else:
            reason = (detail.get("reason") or "").strip()
            if reason:
                notes.append(reason)
    return notes


def rewrite_partially_novel_bullets(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — rewrite_embedding.

    Runs Step-2 rewrite for every bullet whose ``novelty_embedding.judgment``
    is ``REWRITE``.  Writes ``novelty_embedding.rewrite`` and updates ``text``
    (or deactivates the bullet if nothing novel remains).
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_id: str = state["entity_id"]
    entity_name: str = state["entity_name"]
    current_quarter_title: str | None = state.get("current_quarter_title") or None

    start_date = datetime.fromisoformat(state["report_start_date"])
    end_date = datetime.fromisoformat(state["report_end_date"])
    novelty_start = start_date - timedelta(days=settings.NOVELTY_LOOKBACK_DAYS)
    novelty_end = start_date
    current_date = end_date

    bullet_points: list[dict] = state.get("bullet_points") or []

    # Find bullets that need step-2 rewrite
    rewrite_indices = [
        i for i, bp in enumerate(bullet_points)
        if bp.get("is_active", True)
        and ((bp.get("novelty_embedding") or {}).get("judgment") or {}).get("decision") == "rewrite"
    ]

    if not rewrite_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_REWRITE_EMBEDDING,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no REWRITE bullets"},
                ).model_dump()
            ]
        }

    judge = LLMNoveltyJudge(deps.llm_client)

    def rewrite_single(bullet_idx: int):
        bp = bullet_points[bullet_idx]
        record = bullet_to_record(bp)
        original_text = record.text

        judgment = (bp.get("novelty_embedding") or {}).get("judgment") or {}
        evaluator_details: list[dict] = judgment.get("evaluator_details") or []
        reviewer_notes = _extract_reviewer_notes(evaluator_details)

        ctx = NoveltyContext(
            entity_id=entity_id,
            entity_name=entity_name,
            start_date=novelty_start,
            end_date=novelty_end,
            current_date=current_date,
            lookback_days=settings.NOVELTY_LOOKBACK_DAYS,
            clean_up_func=None,
            current_quarter_title=current_quarter_title,
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
            bullet_index=bullet_idx,
        )

        rewritten, decision = judge.run_step2_rewrite(
            original_text=original_text,
            reviewer_notes=reviewer_notes,
            context=ctx,
            bullet_index=bullet_idx,
        )

        return bullet_idx, rewritten, decision

    # Run rewrites in parallel
    rewrite_results: dict[int, tuple[str, str]] = {}
    failures: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(rewrite_single, i): i for i in rewrite_indices}
        for future in as_completed(futures):
            bidx = futures[future]
            try:
                bidx, rewritten_text, decision = future.result()
                rewrite_results[bidx] = (rewritten_text, decision)
            except Exception as e:
                failures[bidx] = e

    updated = list(bullet_points)
    rewrote_count = discarded_count = 0

    for i in rewrite_indices:
        record = bullet_to_record(updated[i])
        original_text = record.text

        if i in failures:
            e = failures[i]
            record.is_active = False
            record.failure = BulletFailure(
                node_id=NODE_REWRITE_EMBEDDING,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            updated[i] = record_to_bullet(record)
            continue

        rewritten_text, decision_raw = rewrite_results.get(i, ("", "discard"))
        decision = decision_raw.lower()

        if record.novelty_embedding is None:
            record.novelty_embedding = NoveltyEmbeddingBlock()

        if decision == "discard" or not rewritten_text.strip():
            # Step 2 returned is_empty — nothing novel remains
            record.is_active = False
            discarded_count += 1
            record.novelty_embedding.rewrite = EmbeddingRewriteMetadata(
                text_before=original_text,
                text_after="",
                is_empty=True,
            )
        else:
            record.text = rewritten_text
            record.novelty_embedding.rewrite = EmbeddingRewriteMetadata(
                text_before=original_text,
                text_after=rewritten_text,
                is_empty=False,
            )
            rewrote_count += 1

        updated[i] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_REWRITE_EMBEDDING,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=len(rewrite_indices),
        extra={
            "rewrote": rewrote_count,
            "discarded_empty": discarded_count,
            "failed_bullets": len(failures),
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

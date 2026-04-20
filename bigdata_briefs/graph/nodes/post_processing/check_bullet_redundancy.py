"""
Node: redundancy_check

Step 8 of the optional bullet post-processing phase.

Calls the brief service's LLM-based redundancy identification and then applies
the plan (KEEP / REWRITE / MERGED / DISCARDED) to the active bullet list.

Only runs when ``ENABLE_BULLET_PROCESSING_PHASE`` is True and there are at
least two active bullets.

Bullets that are merged or discarded are set to ``is_active=False``.
A new ``BulletPointRecord`` is appended for each merged result.

Service type: llm (1-2 LLM calls: identify plan + optional merge/rewrite calls)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_REDUNDANCY_CHECK,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletPointRecord,
    NodeMetricsRecord,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.models import Entity, SingleEntityReport
from bigdata_briefs.settings import settings


def detect_and_merge_redundant_bullets(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — redundancy_check.

    Identifies and resolves redundant bullet points (same facts phrased
    differently).  Returns an updated ``bullet_points`` list.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.ENABLE_BULLET_PROCESSING_PHASE:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_REDUNDANCY_CHECK,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "ENABLE_BULLET_PROCESSING_PHASE=False"},
                ).model_dump()
            ]
        }

    entity = Entity(
        id=state["entity_id"],
        name=state["entity_name"],
        entity_type=state["entity_type"],
        ticker=state.get("entity_ticker") or None,
    )

    bullet_points: list[dict] = state.get("bullet_points") or []
    active_pairs: list[tuple[int, BulletPointRecord]] = [
        (i, bullet_to_record(bp))
        for i, bp in enumerate(bullet_points)
        if bp.get("is_active", True)
    ]

    if len(active_pairs) <= 1:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_REDUNDANCY_CHECK,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "fewer than 2 active bullets"},
                ).model_dump()
            ]
        }

    default_score = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE + 1
    bullets = [r.text for _, r in active_pairs]
    cits = [r.citations for _, r in active_pairs]
    scores = [
        (r.relevance_scoring.score if r.relevance_scoring else default_score)
        for _, r in active_pairs
    ]

    # Build minimal entity_report for the service method
    entity_report = SingleEntityReport(
        entity_id=entity.id,
        entity_info={},
        clean_final_report="",
        report_bulletpoints=bullets,
        bullet_citations=cits,
        relevance_score=scores,
    )

    updated_report, _raw_count = deps.brief_service._apply_validation_bullet_redundancy(
        entity_report,
        entity,
        debug_logger=deps.debug_logger,
        entity_metrics=deps.entity_metrics,
    )

    out_bullets = updated_report.report_bulletpoints
    out_cits = updated_report.bullet_citations
    before_count = len(bullets)
    after_count = len(out_bullets)

    # Map output back to bullet_points
    text_to_orig: dict[str, tuple[int, BulletPointRecord]] = {
        r.text: (orig_i, r) for orig_i, r in active_pairs
    }

    updated = list(bullet_points)
    # Mark all original active bullets as inactive (they will be reinstated or replaced)
    for orig_i, _ in active_pairs:
        rec = bullet_to_record(updated[orig_i])
        rec.is_active = False
        updated[orig_i] = record_to_bullet(rec)

    for out_text, out_cit in zip(out_bullets, out_cits):
        if out_text in text_to_orig:
            # Bullet survived unchanged — reactivate the original record
            orig_i, orig_rec = text_to_orig[out_text]
            orig_rec.is_active = True
            orig_rec.citations = out_cit
            updated[orig_i] = record_to_bullet(orig_rec)
        else:
            # Merged or rewritten bullet with new text — create a new record
            new_rec = BulletPointRecord(
                trace_id=str(uuid4()),
                theme="",
                text=out_text,
                citations=out_cit,
                is_active=True,
            )
            updated.append(record_to_bullet(new_rec))

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_REDUNDANCY_CHECK,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "bullets_before": before_count,
            "bullets_after": after_count,
            "reduction": before_count - after_count,
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

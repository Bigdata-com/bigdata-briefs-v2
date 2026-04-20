"""
Node: thematic_consolidation

Step 9 of the optional bullet post-processing phase.

Clusters active bullets by semantic theme and consolidates multi-bullet groups
into single, concise statements.  Bullets that could not be grouped
(``standalone``) are tagged with ``theme = "__standalone__"`` so the next node
(``standalone_validation``) can process them against the consolidated set.

Only runs when ``ENABLE_BULLET_PROCESSING_PHASE`` is True and there are at
least two active bullets.

After this node:
  - Consolidated bullets: new (or original) ``BulletPointRecord`` objects with
    ``is_active=True`` and their original theme names.
  - Standalone bullets: ``BulletPointRecord`` objects with ``is_active=True``
    and ``theme = "__standalone__"`` — pending review by the next node.
  - Source bullets that were merged: ``is_active=False``.

Service type: llm (cluster + consolidate LLM calls)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_THEMATIC_CONSOLIDATION,
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
from bigdata_briefs.models import ConsolidationMode, Entity
from bigdata_briefs.settings import settings

# Special marker theme for bullets that need standalone validation (Step 10)
_STANDALONE_THEME = "__standalone__"


def cluster_and_consolidate_by_theme(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — thematic_consolidation.

    Groups bullets by theme and merges each multi-bullet group into one
    consolidated statement.  Standalone bullets (not in any group) are tagged
    with ``theme = "__standalone__"`` for further processing.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.ENABLE_BULLET_PROCESSING_PHASE:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_THEMATIC_CONSOLIDATION,
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
                    node_id=NODE_THEMATIC_CONSOLIDATION,
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

    (
        consolidated_bullets, consolidated_cits, _consolidated_scores,
        standalone_bullets, standalone_cits, _standalone_scores,
    ) = deps.brief_service._consolidate_bullets(
        bullets,
        cits,
        scores,
        entity,
        consolidation_mode=ConsolidationMode.LOOSE,
        debug_logger=deps.debug_logger,
        entity_metrics=deps.entity_metrics,
    )

    before_count = len(bullets)
    after_consolidated = len(consolidated_bullets)
    after_standalone = len(standalone_bullets)

    # Build text → original record map for input bullets
    text_to_orig: dict[str, tuple[int, BulletPointRecord]] = {
        r.text: (orig_i, r) for orig_i, r in active_pairs
    }

    updated = list(bullet_points)
    # Mark all original active bullets as inactive initially
    for orig_i, _ in active_pairs:
        rec = bullet_to_record(updated[orig_i])
        rec.is_active = False
        updated[orig_i] = record_to_bullet(rec)

    # Re-activate or create consolidated bullets
    for out_text, out_cit in zip(consolidated_bullets, consolidated_cits):
        if out_text in text_to_orig:
            orig_i, orig_rec = text_to_orig[out_text]
            orig_rec.is_active = True
            orig_rec.citations = out_cit
            updated[orig_i] = record_to_bullet(orig_rec)
        else:
            new_rec = BulletPointRecord(
                trace_id=str(uuid4()),
                theme="",
                text=out_text,
                citations=out_cit,
                is_active=True,
            )
            updated.append(record_to_bullet(new_rec))

    # Tag standalone bullets for Step 10
    for out_text, out_cit in zip(standalone_bullets, standalone_cits):
        if out_text in text_to_orig:
            orig_i, orig_rec = text_to_orig[out_text]
            orig_rec.is_active = True
            orig_rec.theme = _STANDALONE_THEME
            orig_rec.citations = out_cit
            updated[orig_i] = record_to_bullet(orig_rec)
        else:
            new_rec = BulletPointRecord(
                trace_id=str(uuid4()),
                theme=_STANDALONE_THEME,
                text=out_text,
                citations=out_cit,
                is_active=True,
            )
            updated.append(record_to_bullet(new_rec))

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_THEMATIC_CONSOLIDATION,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "bullets_before": before_count,
            "consolidated": after_consolidated,
            "standalone_pending": after_standalone,
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

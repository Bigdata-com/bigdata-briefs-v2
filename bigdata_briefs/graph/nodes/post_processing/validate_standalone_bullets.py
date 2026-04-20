"""
Node: standalone_validation

Step 10 of the optional bullet post-processing phase.

Processes bullets tagged with ``theme = "__standalone__"`` by the previous
``thematic_consolidation`` node.  For each standalone bullet, the LLM decides:
  - KEEP   → bullet is appended to the consolidated set
  - MERGE  → bullet is merged into an existing consolidated bullet
  - REWRITE → bullet is rewritten before being added
  - DISCARD → bullet is dropped (``is_active=False``)

After this node all ``"__standalone__"`` tags are removed (either the bullet
is integrated into the consolidated set or deactivated).

Only runs when ``ENABLE_BULLET_PROCESSING_PHASE`` is True and there are
standalone bullets to process.

Service type: llm (1-N LLM calls: analysis + optional merge/rewrite calls)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_STANDALONE_VALIDATION,
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
from bigdata_briefs.models import Entity
from bigdata_briefs.settings import settings

_STANDALONE_THEME = "__standalone__"


def evaluate_standalone_bullet_actions(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — standalone_validation.

    Evaluates each standalone bullet (``theme == "__standalone__"``) against the
    consolidated bullet set and applies the LLM-prescribed action
    (KEEP / MERGE / REWRITE / DISCARD).
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.ENABLE_BULLET_PROCESSING_PHASE:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_STANDALONE_VALIDATION,
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

    # Separate consolidated and standalone bullets
    consolidated_pairs: list[tuple[int, BulletPointRecord]] = []
    standalone_pairs: list[tuple[int, BulletPointRecord]] = []

    for i, bp in enumerate(bullet_points):
        if not bp.get("is_active", True):
            continue
        rec = bullet_to_record(bp)
        if rec.theme == _STANDALONE_THEME:
            standalone_pairs.append((i, rec))
        else:
            consolidated_pairs.append((i, rec))

    if not standalone_pairs:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_STANDALONE_VALIDATION,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no standalone bullets"},
                ).model_dump()
            ]
        }

    default_score = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE + 1

    cons_texts = [r.text for _, r in consolidated_pairs]
    cons_cits = [r.citations for _, r in consolidated_pairs]
    cons_scores = [
        (r.relevance_scoring.score if r.relevance_scoring else default_score)
        for _, r in consolidated_pairs
    ]
    stand_texts = [r.text for _, r in standalone_pairs]
    stand_cits = [r.citations for _, r in standalone_pairs]
    stand_scores = [
        (r.relevance_scoring.score if r.relevance_scoring else default_score)
        for _, r in standalone_pairs
    ]

    # Call service step 10 — returns (validated_bullets, validated_citations, validated_scores)
    # which is the FINAL combined list (consolidated + kept/merged/rewritten standalones)
    validated_bullets, validated_cits, _validated_scores = (
        deps.brief_service._validate_standalone_bullets(
            cons_texts,
            cons_cits,
            cons_scores,
            stand_texts,
            stand_cits,
            stand_scores,
            entity,
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
        )
    )

    before_total = len(cons_texts) + len(stand_texts)
    after_total = len(validated_bullets)

    # Build text → record map for all currently active bullets
    text_to_orig: dict[str, tuple[int, BulletPointRecord]] = {}
    for orig_i, rec in consolidated_pairs + standalone_pairs:
        text_to_orig[rec.text] = (orig_i, rec)

    updated = list(bullet_points)

    # Deactivate all standalone bullets (they will be re-activated or replaced)
    for orig_i, _ in standalone_pairs:
        rec = bullet_to_record(updated[orig_i])
        rec.is_active = False
        updated[orig_i] = record_to_bullet(rec)

    # Apply validated output
    for out_text, out_cit in zip(validated_bullets, validated_cits):
        if out_text in text_to_orig:
            orig_i, orig_rec = text_to_orig[out_text]
            orig_rec.is_active = True
            orig_rec.theme = orig_rec.theme if orig_rec.theme != _STANDALONE_THEME else ""
            orig_rec.citations = out_cit
            updated[orig_i] = record_to_bullet(orig_rec)
        else:
            # New text (merged or rewritten standalone)
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
        node_id=NODE_STANDALONE_VALIDATION,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "bullets_before": before_total,
            "bullets_after": after_total,
            "standalone_processed": len(standalone_pairs),
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

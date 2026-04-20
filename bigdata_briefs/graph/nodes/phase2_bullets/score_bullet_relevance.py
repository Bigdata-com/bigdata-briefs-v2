"""
Node: relevance_score

Runs parallel relevance checks (LLM, 1-5 scale) for every bullet generated
in the current theme iteration. Bullets that score at or below
``INTRO_SECTION_MIN_RELEVANCE_SCORE`` are immediately marked ``is_active=False``
(discarded before any further processing).

The ``relevance_scoring`` block is written onto each BulletPointRecord for
the current theme. Bullets from previous themes already have this block and
are left untouched.

After scoring, increments ``active_theme_index`` to advance the subgraph loop.

Service type: llm (parallel LLM calls, one per new bullet)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import NODE_RELEVANCE_SCORE, SERVICE_TYPE_LLM
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    NodeMetricsRecord,
    RelevanceScoringMetadata,
    record_to_bullet,
    bullet_to_record,
)
from bigdata_briefs.models import Entity, ReportDates, RelevanceCheckResult
from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
from bigdata_briefs.settings import settings


def score_and_gate_bullet_relevance(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — relevance_score.

    Scores each unscored bullet (those without a ``relevance_scoring`` block)
    for the current theme and updates the ``BulletPointRecord`` in place.
    Bullets that do not meet the threshold are deactivated.

    Increments ``active_theme_index`` at the end so the subgraph loop either
    advances to the next theme or exits.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    theme_index: int = state.get("active_theme_index", 0)
    themes: list[str] = state.get("themes", [])
    current_theme = themes[theme_index] if theme_index < len(themes) else ""

    entity = Entity(
        id=state["entity_id"],
        name=state["entity_name"],
        entity_type=state["entity_type"],
        ticker=state.get("entity_ticker") or None,
    )
    report_dates = ReportDates(
        start=state["report_start_date"],
        end=state["report_end_date"],
    )
    current_quarter_title = state.get("current_quarter_title") or "N/A"
    entity_info_str = f"{entity.name} ({entity.ticker})" if entity.ticker else entity.name
    current_dt = report_dates.get_current_date_for_prompt()
    threshold = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE
    default_score = threshold + 1

    relevance_prompt = get_prompt_keys("relevance_check")

    # Only score bullets that belong to the current theme and haven't been scored yet
    bullet_points: list[dict] = state.get("bullet_points") or []
    unscored_indices = [
        i for i, bp in enumerate(bullet_points)
        if bp.get("theme") == current_theme and bp.get("relevance_scoring") is None
    ]

    def run_one_relevance(bullet_idx: int, bullet_text: str):
        user_content = relevance_prompt.user_template.render(
            entity_info=entity_info_str,
            current_datetime=current_dt,
            contextual_quarter=current_quarter_title,
            bullet_text=bullet_text,
            response_format=f"{RelevanceCheckResult.model_json_schema()}",
        )
        result = deps.llm_client.call_with_response_format(
            system=[{"role": "system", "content": relevance_prompt.system_prompt}],
            messages=[{"role": "user", "content": user_content}],
            text_format=RelevanceCheckResult,
            step_name=f"relevance_score_{current_theme.replace(' ', '_')}_{bullet_idx}",
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
            **relevance_prompt.llm_kwargs,
        )
        return (bullet_idx, result.relevance_score, result.reason)

    # Run relevance checks in parallel for all unscored bullets
    score_map: dict[int, tuple[int, str]] = {}
    failures: dict[int, Exception] = {}
    if unscored_indices:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(run_one_relevance, i, bullet_points[i]["text"]): i
                for i in unscored_indices
            }
            for future in as_completed(futures):
                bidx = futures[future]
                try:
                    idx, score, reason = future.result()
                    score_map[idx] = (score, reason)
                except Exception as e:
                    failures[bidx] = e

    # Update BulletPointRecord objects
    updated = list(bullet_points)
    scored_count = 0
    discarded_count = 0

    for i in unscored_indices:
        record = bullet_to_record(updated[i])

        if i in failures:
            e = failures[i]
            record.is_active = False
            record.failure = BulletFailure(
                node_id=NODE_RELEVANCE_SCORE,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            updated[i] = record_to_bullet(record)
            continue

        score, reason = score_map.get(i, (default_score, "not scored"))
        passed = score > threshold
        record.relevance_scoring = RelevanceScoringMetadata(
            score=score,
            reason=reason,
            passed=passed,
        )
        if not passed:
            record.is_active = False
            discarded_count += 1
        else:
            scored_count += 1
        updated[i] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_RELEVANCE_SCORE,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=len(unscored_indices),
        extra={
            "theme": current_theme,
            "theme_index": theme_index,
            "scored": scored_count,
            "discarded_by_relevance": discarded_count,
            "failed_bullets": len(failures),
        },
    )

    return {
        "bullet_points": updated,
        # Advance index — the subgraph edge function uses this to decide loop vs exit
        "active_theme_index": theme_index + 1,
        "node_metrics": [metrics.model_dump()],
    }

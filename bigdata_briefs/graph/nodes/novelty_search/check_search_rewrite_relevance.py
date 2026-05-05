"""
Node: relevance_score_search

Post-search relevance gate for bullets rewritten by ``novelty_via_search``.

For every active bullet whose ``novelty_search.search.verdict`` is ``rewrite``
and whose ``rewritten_text`` is non-empty, this node runs a brief-side
``relevance_check`` LLM call.  Bullets that fall at or below
``INTRO_SECTION_MIN_RELEVANCE_SCORE`` are deactivated.

Only runs when ``NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED`` is True.

Service type: llm (parallel LLM calls, one per search-rewritten bullet)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_RELEVANCE_SCORE_SEARCH,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    NodeMetricsRecord,
    NoveltySearchBlock,
    SearchRelevanceMetadata,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.graph.nodes.novelty_search._search_impl import run_pivot_relevance_check
from bigdata_briefs.novelty.novelty_service import run_relevance_check_for_bullet_text
from bigdata_briefs.novelty.step_names import novelty_search_rewrite_relevance_check_step_name

_PIVOT_VERDICTS = {"mixed", "single_partially_novel", "mixed_partial", "multi_partially_novel"}
from bigdata_briefs.settings import settings


def score_search_rewrite_relevance(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — relevance_score_search.

    Scores rewritten bullets from the novelty-via-search phase using the
    brief-side ``relevance_check`` prompt.  Bullets below the threshold are
    deactivated.

    Writes ``novelty_search.relevance_check`` on each assessed bullet.
    Skips entirely when ``NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED`` is False.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_RELEVANCE_SCORE_SEARCH,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED=False"},
                ).model_dump()
            ]
        }

    entity_name: str = state["entity_name"]
    entity_ticker: str | None = state.get("entity_ticker") or None
    current_quarter_title: str | None = state.get("current_quarter_title") or None

    start_date = datetime.fromisoformat(state["report_start_date"])
    current_datetime_str = start_date.strftime("%A, %B %d, %Y")

    threshold = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE
    default_score = threshold + 1

    bullet_points: list[dict] = state.get("bullet_points") or []

    # Find active bullets that were rewritten by novelty_via_search
    check_indices = [
        i for i, bp in enumerate(bullet_points)
        if bp.get("is_active", True)
        and ((bp.get("novelty_search") or {}).get("search") or {}).get("verdict") == "rewrite"
        and ((bp.get("novelty_search") or {}).get("search") or {}).get("rewritten_text", "").strip()
    ]

    if not check_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_RELEVANCE_SCORE_SEARCH,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no search-rewritten bullets"},
                ).model_dump()
            ]
        }

    def check_single(bullet_idx: int) -> tuple[int, int, str | None]:
        bp = bullet_points[bullet_idx]
        search_block = (bp.get("novelty_search") or {}).get("search") or {}
        rewritten_text: str = search_block.get("rewritten_text", "")
        overall_verdict: str = search_block.get("overall_verdict") or ""
        step_name = novelty_search_rewrite_relevance_check_step_name(bullet_idx)

        # Pivot-rewritten bullets (mixed / single_partially_novel) get a dedicated
        # relevance check that focuses only on the new detail added after the pivot
        # marker, ignoring the known subordinate context clause.
        if overall_verdict in _PIVOT_VERDICTS:
            score, reasoning = run_pivot_relevance_check(
                rewritten_sentence=rewritten_text,
                entity_name=entity_name,
                llm_client=deps.llm_client,
                step_name=step_name,
                debug_logger=deps.debug_logger,
                entity_metrics=deps.entity_metrics,
                default_score=threshold + 1,
            )
        else:
            score, reasoning = run_relevance_check_for_bullet_text(
                rewritten_text=rewritten_text,
                entity_name=entity_name,
                entity_ticker=entity_ticker,
                current_datetime_str=current_datetime_str,
                current_quarter_title=current_quarter_title,
                llm_client=deps.llm_client,
                debug_logger=deps.debug_logger,
                entity_metrics=deps.entity_metrics,
                step_name=step_name,
                bullet_index=bullet_idx,
            )
        return bullet_idx, score, reasoning

    max_workers = min(
        settings.NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT,
        len(check_indices),
    )
    score_map: dict[int, tuple[int, str | None]] = {}
    failures: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_single, i): i for i in check_indices}
        for future in as_completed(futures):
            bidx = futures[future]
            try:
                bidx, score, reasoning = future.result()
                score_map[bidx] = (score, reasoning)
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
                node_id=NODE_RELEVANCE_SCORE_SEARCH,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            updated[i] = record_to_bullet(record)
            continue

        score, reasoning = score_map.get(i, (default_score, None))
        passed = score > threshold

        if record.novelty_search is None:
            record.novelty_search = NoveltySearchBlock()

        record.novelty_search.relevance_check = SearchRelevanceMetadata(
            score=score,
            passed=passed,
            reasoning=reasoning,
        )
        if not passed:
            record.is_active = False
            failed_count += 1
        else:
            passed_count += 1

        updated[i] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_RELEVANCE_SCORE_SEARCH,
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

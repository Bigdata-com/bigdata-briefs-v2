"""
Node: novelty_search_parse_and_plan

Decompone ogni bullet attivo in claim atomici e genera search query.
Usa LLMClient sync con ThreadPoolExecutor.
Risultati → deps._search_cache[trace_id] (chiave "claims", "sentence_parts").

Service type: llm
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs import logger
from bigdata_briefs.graph.constants import (
    NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NS_MAX_TOKENS,
    _NS_MODEL,
    _NS_REASONING_EFFORT,
    _NSParseAndPlanResponse,
    _PARSE_AND_PLAN_PROMPT,
    _ns_validate_parse_and_plan_response,
)
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    NodeMetricsRecord,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.settings import settings


def parse_and_plan_search(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — novelty_search_parse_and_plan.

    For every active bullet, calls the LLM to decompose the sentence into
    atomic claims and focused search queries.  Results are stored in
    ``deps._search_cache[trace_id]`` under keys ``"claims"`` and
    ``"sentence_parts"``.

    Returns only ``node_metrics`` — bullet_points state is not modified here.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.NOVELTY_SEARCH_ENABLED:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "NOVELTY_SEARCH_ENABLED=False"},
                ).model_dump()
            ]
        }

    entity_name: str = state["entity_name"]
    bullet_points: list[dict] = state.get("bullet_points") or []
    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    if not active_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets"},
                ).model_dump()
            ]
        }

    # Collect (bullet_idx, trace_id, sentence) for active bullets
    active_entries: list[tuple[int, str, str]] = []
    for i in active_indices:
        record = bullet_to_record(bullet_points[i])
        active_entries.append((i, record.trace_id, record.text or ""))

    updated = list(bullet_points)
    success_count = failure_count = 0
    max_workers = max(1, settings.NOVELTY_SEARCH_MAX_CONCURRENT)

    def _parse_one(bullet_idx: int, trace_id: str, sentence: str) -> None:
        """Parse one bullet — stores result in cache or marks bullet inactive."""
        user_content = _PARSE_AND_PLAN_PROMPT.format(
            sentence=sentence,
            entity=entity_name,
        )
        parse_result: _NSParseAndPlanResponse | None = deps.llm_client.call_with_response_format(
            system=[],
            messages=[{"role": "user", "content": user_content}],
            text_format=_NSParseAndPlanResponse,
            model=_NS_MODEL,
            max_tokens=_NS_MAX_TOKENS,
            reasoning_effort=_NS_REASONING_EFFORT,
            step_name=f"novelty_search_parse_{bullet_idx}",
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
        )
        if parse_result is None:
            raise RuntimeError(
                "parse_and_plan returned None (LLM produced no parseable output)"
            )
        _ns_validate_parse_and_plan_response(parse_result, entity_name)
        deps.store_search_data(trace_id, "claims", parse_result.claims)
        deps.store_search_data(trace_id, "sentence_parts", parse_result.sentence_parts)
        logger.info(
            "[novelty_search_parse] bullet=%d claims=%d queries=%d",
            bullet_idx,
            len(parse_result.claims),
            len(parse_result.sentence_parts),
        )

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="ns-parse",
    ) as executor:
        future_to_entry = {
            executor.submit(_parse_one, bullet_idx, trace_id, sentence): (bullet_idx, trace_id)
            for bullet_idx, trace_id, sentence in active_entries
        }
        for future in as_completed(future_to_entry):
            bullet_idx, trace_id = future_to_entry[future]
            try:
                future.result()
                success_count += 1
            except Exception as exc:
                logger.warning(
                    "[novelty_search_parse] bullet=%d FAILED: %s",
                    bullet_idx,
                    exc,
                )
                record = bullet_to_record(updated[bullet_idx])
                record.is_active = False
                record.failure = BulletFailure(
                    node_id=NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                updated[bullet_idx] = record_to_bullet(record)
                failure_count += 1

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=success_count,
        extra={
            "bullets_parsed": success_count,
            "bullets_failed": failure_count,
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

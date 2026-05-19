"""
Node: novelty_search_judgment

Valuta la novelty di ogni claim per ogni bullet attivo.
Un LLM call per claim (seriale nel thread). ThreadPoolExecutor per i bullet.
Risultati → deps._search_cache[trace_id] (chiave "claim_verdicts", "overall_verdict").

Service type: llm
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs import logger
from bigdata_briefs.graph.constants import (
    NODE_NOVELTY_SEARCH_JUDGMENT,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NS_MAX_TOKENS,
    _NS_MODEL,
    _NS_REASONING_EFFORT,
    _NSClaim,
    _NSClaimVerdict,
    _NSSentencePart,
    _NSSearchResult,
    _NSSingleClaimVerdictResponse,
    _SINGLE_CLAIM_NOVELTY_PROMPT,
    _ns_compute_overall_verdict,
    _ns_format_evidence_grouped_by_date_and_doc,
    _ns_get_evidence_for_claim,
    _ns_timestamp_to_date,
)
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.settings import settings


def judge_novelty_by_search(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — novelty_search_judgment.

    For every active bullet that has both parse data and search results in the
    cache, evaluates the novelty of each atomic claim via an LLM call (serial
    within the bullet's worker thread).  Results are stored in
    ``deps._search_cache[trace_id]`` under keys ``"claim_verdicts"`` and
    ``"overall_verdict"``.

    Returns only ``node_metrics``.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not settings.NOVELTY_SEARCH_ENABLED:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_JUDGMENT,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "NOVELTY_SEARCH_ENABLED=False"},
                ).model_dump()
            ]
        }

    entity_name: str = state["entity_name"]
    reference_date_iso: str = state["report_start_date"] or ""
    bullet_points: list[dict] = state.get("bullet_points") or []
    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    if not active_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_JUDGMENT,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets"},
                ).model_dump()
            ]
        }

    reference_date_display = _ns_timestamp_to_date(reference_date_iso)

    # Collect entries that have parse data in cache
    active_entries: list[tuple[int, str, str]] = []
    for i in active_indices:
        record = bullet_to_record(bullet_points[i])
        if deps.get_search_data(record.trace_id, "claims") is None:
            logger.debug(
                "[novelty_search_judgment] bullet=%d no parse data — skipping",
                i,
            )
            continue
        active_entries.append((i, record.trace_id, record.text or ""))

    if not active_entries:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_JUDGMENT,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no cache entries available"},
                ).model_dump()
            ]
        }

    success_count = failure_count = 0
    total_llm_calls = 0
    max_workers = max(1, settings.NOVELTY_SEARCH_MAX_CONCURRENT)

    def _judge_one(bullet_idx: int, trace_id: str, sentence: str) -> int:
        """Judge all claims for one bullet; returns number of LLM calls made."""
        claims: list[_NSClaim] = deps.get_search_data(trace_id, "claims")
        sentence_parts: list[_NSSentencePart] = deps.get_search_data(trace_id, "sentence_parts")
        merged_results: list[_NSSearchResult] | None = deps.get_search_data(trace_id, "merged_results")
        results_per_part: list[list[_NSSearchResult]] | None = deps.get_search_data(trace_id, "results_per_part")

        # Fetch failed → discard (no evidence due to error, not genuine absence)
        fetch_error = deps.get_search_data(trace_id, "fetch_error")
        if merged_results is None and fetch_error:
            deps.store_search_data(trace_id, "claim_verdicts", [])
            deps.store_search_data(trace_id, "overall_verdict", "discard_step_error")
            deps.store_search_data(trace_id, "step_error_reason", f"Search fetch failed: {fetch_error}")
            logger.warning(
                "[novelty_search_judgment] bullet=%d fetch error → discard: %s",
                bullet_idx,
                fetch_error,
            )
            return 0

        # No evidence (search succeeded but returned nothing) → all claims are novel
        if not merged_results:
            claim_verdicts: list[_NSClaimVerdict] = [
                _NSClaimVerdict(
                    claim_index=i,
                    novelty="novel",
                    evidence_ids=[],
                    reasoning="No prior evidence found to contradict this claim.",
                )
                for i in range(len(claims))
            ]
            overall_verdict = "novel"
            deps.store_search_data(trace_id, "claim_verdicts", claim_verdicts)
            deps.store_search_data(trace_id, "overall_verdict", overall_verdict)
            logger.info(
                "[novelty_search_judgment] bullet=%d no evidence → all novel",
                bullet_idx,
            )
            return 0

        claim_verdicts = []
        llm_calls = 0

        for i, claim in enumerate(claims):
            claim_evidence = _ns_get_evidence_for_claim(
                claim_index=i,
                sentence_parts=sentence_parts or [],
                results_per_part=results_per_part or [],
                all_results=merged_results,
            )
            evidence_text = _ns_format_evidence_grouped_by_date_and_doc(claim_evidence)

            user_content = _SINGLE_CLAIM_NOVELTY_PROMPT.format(
                sentence=sentence,
                entity=entity_name,
                reference_date=reference_date_display,
                claim_text=claim.text,
                evidence_text=evidence_text,
            )
            verdict_response: _NSSingleClaimVerdictResponse | None = (
                deps.llm_client.call_with_response_format(
                    system=[],
                    messages=[{"role": "user", "content": user_content}],
                    text_format=_NSSingleClaimVerdictResponse,
                    model=_NS_MODEL,
                    max_tokens=_NS_MAX_TOKENS,
                    reasoning_effort=_NS_REASONING_EFFORT,
                    step_name=f"novelty_search_judgment_{bullet_idx}_c{i}",
                    debug_logger=deps.debug_logger,
                    entity_metrics=deps.entity_metrics,
                )
            )
            if verdict_response is None:
                raise RuntimeError(
                    f"novelty_judgment returned None for claim {i} of bullet {bullet_idx}"
                )
            llm_calls += 1
            claim_verdicts.append(
                _NSClaimVerdict(
                    claim_index=i,
                    novelty=verdict_response.novelty,
                    evidence_ids=verdict_response.evidence_ids,
                    reasoning=verdict_response.reasoning,
                )
            )

        overall_verdict = _ns_compute_overall_verdict(claim_verdicts)
        deps.store_search_data(trace_id, "claim_verdicts", claim_verdicts)
        deps.store_search_data(trace_id, "overall_verdict", overall_verdict)

        logger.info(
            "[novelty_search_judgment] bullet=%d claims=%d overall_verdict=%r",
            bullet_idx,
            len(claims),
            overall_verdict,
        )
        return llm_calls

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="ns-judge",
    ) as executor:
        future_to_entry = {
            executor.submit(_judge_one, bullet_idx, trace_id, sentence): (bullet_idx, trace_id)
            for bullet_idx, trace_id, sentence in active_entries
        }
        for future in as_completed(future_to_entry):
            bullet_idx, trace_id = future_to_entry[future]
            try:
                calls = future.result()
                total_llm_calls += calls
                success_count += 1
            except Exception as exc:
                logger.warning(
                    "[novelty_search_judgment] bullet=%d FAILED: %s",
                    bullet_idx,
                    exc,
                )
                deps.store_search_data(trace_id, "claim_verdicts", [])
                deps.store_search_data(trace_id, "overall_verdict", "discard_step_error")
                deps.store_search_data(trace_id, "step_error_reason", f"Judgment failed: {exc}")
                failure_count += 1

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_NOVELTY_SEARCH_JUDGMENT,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=total_llm_calls,
        extra={
            "bullets_judged": success_count,
            "bullets_failed": failure_count,
        },
    )

    return {"node_metrics": [metrics.model_dump()]}

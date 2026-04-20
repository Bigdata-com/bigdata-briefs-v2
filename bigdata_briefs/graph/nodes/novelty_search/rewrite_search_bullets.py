"""
Node: novelty_search_rewrite

Decide keep/rewrite/discard per ogni bullet attivo.
Scrive il risultato finale in state (NoveltySearchBlock.search).
Applica il verdetto (deattiva/aggiorna testo).
Svuota deps._search_cache alla fine.

Service type: llm
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs import logger
from bigdata_briefs.graph.constants import (
    NODE_NOVELTY_SEARCH_REWRITE,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NS_MAX_TOKENS,
    _NS_MODEL,
    _NS_REASONING_EFFORT,
    _NSClaim,
    _NSClaimVerdict,
    _NSRewriteResponse,
    _NSSearchResult,
    _REWRITE_PROMPT,
    _ns_build_rewrite_prompt_sections,
)
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    NodeMetricsRecord,
    NoveltySearchBlock,
    SearchNoveltyMetadata,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.settings import settings


def rewrite_search_bullets(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — novelty_search_rewrite.

    For every active bullet that has verdict data in the search cache, calls
    the LLM to decide keep / rewrite / discard.  Applies the verdict:
      - keep    → bullet text unchanged, metadata written
      - rewrite → bullet text updated to rewritten sentence
      - discard → ``is_active = False``

    Writes ``NoveltySearchBlock.search`` to each processed bullet.

    Clears ``deps._search_cache`` after the loop.

    Also calls ``deps.debug_logger.save_novelty_search_langgraph_batch`` for
    the full batch debug log.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_id: str = state["entity_id"]
    entity_name: str = state["entity_name"]
    reference_date_iso: str = state["report_start_date"] or ""
    bullet_points: list[dict] = state.get("bullet_points") or []
    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    if not settings.NOVELTY_SEARCH_ENABLED or not active_indices:
        reason = (
            "NOVELTY_SEARCH_ENABLED=False"
            if not settings.NOVELTY_SEARCH_ENABLED
            else "no active bullets"
        )
        wall_ms = (time.monotonic() - t0) * 1000
        deps.clear_search_cache()
        return {
            "bullet_points": bullet_points,
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_REWRITE,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": reason},
                ).model_dump()
            ],
        }

    # Collect entries that have verdict data in the cache
    active_entries: list[tuple[int, str, str]] = []
    for i in active_indices:
        record = bullet_to_record(bullet_points[i])
        if deps.get_search_data(record.trace_id, "claim_verdicts") is None:
            logger.debug(
                "[novelty_search_rewrite] bullet=%d no verdict data in cache — skipping",
                i,
            )
            continue
        active_entries.append((i, record.trace_id, record.text or ""))

    updated = list(bullet_points)
    results: dict[int, dict | Exception] = {}
    max_workers = max(1, settings.NOVELTY_SEARCH_MAX_CONCURRENT)

    def _rewrite_one(bullet_idx: int, trace_id: str, sentence: str) -> dict:
        """Rewrite one bullet; returns result dict."""
        claims: list[_NSClaim] = deps.get_search_data(trace_id, "claims")
        claim_verdicts: list[_NSClaimVerdict] = deps.get_search_data(trace_id, "claim_verdicts")
        overall_verdict: str = deps.get_search_data(trace_id, "overall_verdict") or "old"
        merged_results: list[_NSSearchResult] = deps.get_search_data(trace_id, "merged_results") or []

        id_to_chunk = {r.simple_id: r for r in merged_results}
        claims_and_verdicts_text, all_evidence_text, reasonings_text = (
            _ns_build_rewrite_prompt_sections(claims, claim_verdicts, id_to_chunk)
        )

        user_content = _REWRITE_PROMPT.format(
            sentence=sentence,
            claims_and_verdicts=claims_and_verdicts_text,
            all_evidence=all_evidence_text,
            reasonings_per_claim=reasonings_text,
        )
        rewrite_response: _NSRewriteResponse | None = deps.llm_client.call_with_response_format(
            system=[],
            messages=[{"role": "user", "content": user_content}],
            text_format=_NSRewriteResponse,
            model=_NS_MODEL,
            max_tokens=_NS_MAX_TOKENS,
            reasoning_effort=_NS_REASONING_EFFORT,
            step_name=f"novelty_search_rewrite_{bullet_idx}",
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
        )
        if rewrite_response is None:
            raise RuntimeError(
                f"rewrite returned None for bullet {bullet_idx}"
            )

        action = rewrite_response.action
        rewritten_sentence = rewrite_response.rewritten_sentence
        if action == "keep":
            rewritten_sentence = sentence
        elif action == "discard":
            rewritten_sentence = None

        logger.info(
            "[novelty_search_rewrite] bullet=%d action=%r overall_verdict=%r",
            bullet_idx,
            action,
            overall_verdict,
        )
        return {
            "rewrite_action": action,
            "rewritten_sentence": rewritten_sentence,
            "overall_verdict": overall_verdict,
            "reason": rewrite_response.reasoning,
            "verdict_reason": rewrite_response.reasoning,
            "overall_verdict_reason": rewrite_response.reasoning,
            "search_queries": [p.search_query for p in (deps.get_search_data(trace_id, "sentence_parts") or [])],
            "results_count": len(merged_results),
            "claims": [c.model_dump() for c in claims],
            "claim_verdicts": [v.model_dump() for v in claim_verdicts],
        }

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="ns-rewrite",
    ) as executor:
        future_to_entry = {
            executor.submit(_rewrite_one, bullet_idx, trace_id, sentence): (bullet_idx, trace_id)
            for bullet_idx, trace_id, sentence in active_entries
        }
        for future in as_completed(future_to_entry):
            bullet_idx, trace_id = future_to_entry[future]
            pos = next(
                p for p, (idx, _, _) in enumerate(active_entries) if idx == bullet_idx
            )
            try:
                results[bullet_idx] = future.result()
            except Exception as exc:
                logger.warning(
                    "[novelty_search_rewrite] bullet=%d FAILED: %s",
                    bullet_idx,
                    exc,
                )
                results[bullet_idx] = exc

    # Apply results to bullet_points
    keep_count = discard_count = rewrite_count = error_count = 0

    sentences_for_debug = [sentence for _, _, sentence in active_entries]
    pipe_results_for_debug: list[dict | BaseException] = [
        results.get(bullet_idx, Exception("missing result"))
        for bullet_idx, _, _ in active_entries
    ]

    for bullet_idx, trace_id, original_text in active_entries:
        result = results.get(bullet_idx)
        record = bullet_to_record(updated[bullet_idx])

        if isinstance(result, Exception) or not isinstance(result, dict):
            record.is_active = False
            error_count += 1
            error_msg = (
                str(result)
                if isinstance(result, Exception)
                else f"unexpected result type: {type(result).__name__}"
            )
            record.failure = BulletFailure(
                node_id=NODE_NOVELTY_SEARCH_REWRITE,
                error_type=type(result).__name__ if isinstance(result, Exception) else "UnexpectedResultType",
                error_message=error_msg,
            )
            updated[bullet_idx] = record_to_bullet(record)
            continue

        action = result.get("rewrite_action") or "keep"
        rewritten_sentence = result.get("rewritten_sentence")

        verdict = action if action in ("keep", "discard", "rewrite") else "keep"
        final_text = (
            rewritten_sentence if (verdict == "rewrite" and rewritten_sentence) else None
        )

        reason: str | None = (
            result.get("reason")
            or result.get("verdict_reason")
            or result.get("overall_verdict_reason")
        )

        # Sanitized copy of result for debugging (strip large fields)
        _skip_keys = {"sentence", "embedding", "embeddings", "chunks"}
        details: dict | None = {
            k: v
            for k, v in result.items()
            if k not in _skip_keys and not isinstance(v, (bytes, bytearray))
        } or None

        record.novelty_search = NoveltySearchBlock(
            search=SearchNoveltyMetadata(
                verdict=verdict,
                rewritten_text=final_text,
                duration_seconds=0.0,
                reason=reason,
                details=details,
                overall_verdict=result.get("overall_verdict"),
            )
        )

        if verdict == "discard":
            record.is_active = False
            discard_count += 1
        elif verdict == "rewrite" and final_text:
            record.text = final_text
            rewrite_count += 1
        else:
            keep_count += 1

        updated[bullet_idx] = record_to_bullet(record)

    # Debug log for the full batch
    if deps.debug_logger is not None:
        deps.debug_logger.save_novelty_search_langgraph_batch(
            entity_id=entity_id,
            entity_name=entity_name,
            reference_date=reference_date_iso,
            sentences=sentences_for_debug,
            pipe_results=pipe_results_for_debug,
        )

    # Clear the search cache — no longer needed after this node
    deps.clear_search_cache()

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_NOVELTY_SEARCH_REWRITE,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=keep_count + discard_count + rewrite_count,
        extra={
            "keep": keep_count,
            "discard": discard_count,
            "rewrite": rewrite_count,
            "failed_bullets": error_count,
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

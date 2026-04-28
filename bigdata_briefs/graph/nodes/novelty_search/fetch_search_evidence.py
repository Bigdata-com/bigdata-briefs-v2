"""
Node: novelty_search_fetch

Esegue la Bigdata.com search per ogni bullet attivo.
asyncio.run() per thread — event loop fresco per ogni worker thread.
Risultati → deps._search_cache[trace_id] (chiave "results_per_part", "merged_results").

Service type: search
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs import logger
from bigdata_briefs.graph.constants import (
    NODE_NOVELTY_SEARCH_FETCH,
    SERVICE_TYPE_SEARCH,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.nodes.novelty_search._search_impl import (
    _NSSentencePart,
    _ns_multi_query_search,
)
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.settings import settings


def fetch_search_evidence(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — novelty_search_fetch.

    For every active bullet whose parse data is available in the search cache,
    executes the Bigdata.com multi-query search (via asyncio.run inside each
    worker thread).  Results are stored in
    ``deps._search_cache[trace_id]`` under keys ``"results_per_part"`` and
    ``"merged_results"``.

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
                    node_id=NODE_NOVELTY_SEARCH_FETCH,
                    service_type=SERVICE_TYPE_SEARCH,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "NOVELTY_SEARCH_ENABLED=False"},
                ).model_dump()
            ]
        }

    entity_id: str = state["entity_id"]
    reference_date_iso: str = state["report_start_date"] or ""
    bullet_points: list[dict] = state.get("bullet_points") or []
    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    if not active_indices:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_FETCH,
                    service_type=SERVICE_TYPE_SEARCH,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets"},
                ).model_dump()
            ]
        }

    bigdata_api_key = str(settings.BIGDATA_API_KEY)
    request_hook = (
        deps.bigdata_rate_limiter.aacquire if deps.bigdata_rate_limiter is not None else None
    )

    # Collect entries that have parse data in the cache
    active_entries: list[tuple[int, str]] = []
    for i in active_indices:
        record = bullet_to_record(bullet_points[i])
        sentence_parts = deps.get_search_data(record.trace_id, "sentence_parts")
        if sentence_parts is None:
            logger.debug(
                "[novelty_search_fetch] bullet=%d no parse data in cache — skipping",
                i,
            )
            continue
        active_entries.append((i, record.trace_id))

    if not active_entries:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_NOVELTY_SEARCH_FETCH,
                    service_type=SERVICE_TYPE_SEARCH,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no cache entries available"},
                ).model_dump()
            ]
        }

    success_count = failure_count = 0
    total_query_units = 0.0
    total_chunks_fetched = 0
    max_workers = max(1, settings.NOVELTY_SEARCH_MAX_CONCURRENT)

    def _fetch_one(bullet_idx: int, trace_id: str) -> tuple[float, int]:
        """Run search for one bullet; returns (query_units, chunk_count)."""
        sentence_parts: list[_NSSentencePart] = deps.get_search_data(trace_id, "sentence_parts")
        search_queries = [p.search_query for p in sentence_parts]

        # asyncio.run() creates a fresh event loop per worker thread — safe
        results_per_part, merged_results, _, query_units = asyncio.run(
            _ns_multi_query_search(
                search_queries=search_queries,
                entity_id=entity_id,
                reference_date=reference_date_iso,
                api_key=bigdata_api_key,
                request_hook=request_hook,
            )
        )

        deps.store_search_data(trace_id, "results_per_part", results_per_part)
        deps.store_search_data(trace_id, "merged_results", merged_results)

        logger.info(
            "[novelty_search_fetch] bullet=%d evidence=%d units=%.3f",
            bullet_idx,
            len(merged_results),
            query_units,
        )
        return query_units, len(merged_results)

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="ns-fetch",
    ) as executor:
        future_to_entry = {
            executor.submit(_fetch_one, bullet_idx, trace_id): (bullet_idx, trace_id)
            for bullet_idx, trace_id in active_entries
        }
        for future in as_completed(future_to_entry):
            bullet_idx, trace_id = future_to_entry[future]
            try:
                units, chunk_count = future.result()
                total_query_units += units
                total_chunks_fetched += chunk_count
                success_count += 1
            except Exception as exc:
                logger.warning(
                    "[novelty_search_fetch] bullet=%d FAILED: %s",
                    bullet_idx,
                    exc,
                )
                # Do not deactivate bullet here — judgment will skip on empty cache
                failure_count += 1

    if deps.entity_metrics:
        if total_chunks_fetched:
            deps.entity_metrics.track_chunks(total_chunks_fetched, attributee_step="novelty_search_fetch")
        deps.entity_metrics.track_api_call(
            success_count, total_query_units, attributee_step="novelty_search_fetch"
        )

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_NOVELTY_SEARCH_FETCH,
        service_type=SERVICE_TYPE_SEARCH,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        search_calls=success_count,
        extra={
            "bullets_fetched": success_count,
            "bullets_failed": failure_count,
            "total_query_units": round(total_query_units, 4),
            "total_chunks_fetched": total_chunks_fetched,
        },
    )

    return {"node_metrics": [metrics.model_dump()]}

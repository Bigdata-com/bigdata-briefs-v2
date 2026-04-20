"""
Node: concept_search_postprocessing

Post-processes raw concept search results:
  - Deduplicates by doc_id + chunk_num
  - Applies hash-based filtering (excludes chunks seen in recent runs)
  - Stores new chunk hashes for future deduplication
  - Optionally reranks results by source_rank

Also builds the initial ``source_references`` dict that maps ref_id strings
to serialized SourceChunkReference data, which nodes downstream use to look
up citation details.

Service type: none (local I/O — SQLite hash storage + in-memory processing)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    NODE_CONCEPT_SEARCH_POSTPROCESSING,
    PIPELINE_STATUS_NO_DATA,
    SERVICE_TYPE_NONE,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.models import ConceptExtraction, Entity, ReportDates, Result


def deduplicate_and_filter_concept_results(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — concept_search_postprocessing.

    Runs deduplication, hash filtering, optional reranking on the raw concept
    search results, then builds the ``source_references`` index from all
    surviving chunks.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

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
    cfg = state.get("config") or {}

    raw = state["raw_concept_results"]

    all_results: list[Result] = [Result.model_validate(r) for r in raw.get("all_results", [])]
    results_per_concept: dict = {
        concept: {
            "theme": data["theme"],
            "results": [Result.model_validate(r) for r in data["results"]],
        }
        for concept, data in raw.get("results_per_concept", {}).items()
    }
    results_by_theme_raw: dict = {
        theme: [Result.model_validate(r) for r in items]
        for theme, items in raw.get("results_by_theme", {}).items()
    }
    concepts = ConceptExtraction.model_validate(raw["concepts"])

    rerank_concept_sources = cfg.get("rerank_concept_sources", False)
    store_retrieved_chunks = cfg.get("store_retrieved_chunks")

    deduplicated, processed_by_theme = deps.query_service.process_concept_results(
        entity=entity,
        concepts=concepts,
        all_results=all_results,
        results_per_concept=results_per_concept,
        results_by_theme=results_by_theme_raw,
        report_dates=report_dates,
        rerank_concept_sources=rerank_concept_sources,
        store_retrieved_chunks=store_retrieved_chunks,
        debug_logger=deps.debug_logger,
    )

    # Build source_references: ref_id -> SourceChunkReference serialized dict.
    # Each chunk in each result gets a unique ref_id (e.g. "CQS:REF0").
    source_references: dict = {}
    ref_counter = 0
    for result in deduplicated:
        for chunk in result.chunks:
            ref_id = f"CQS:REF{ref_counter}"
            ref_counter += 1
            source_references[ref_id] = {
                "ref_id": ref_id,
                "document_id": result.document_id,
                "headline": result.headline,
                "ts": result.timestamp,
                "source_name": result.source_name,
                "source_rank": result.source_rank,
                "url": result.url,
                "chunk_id": chunk.chunk,
                "text": chunk.text,
                "highlights": [h.model_dump() for h in chunk.highlights],
                "_is_referenced": False,
            }

    processed_concept_results = {
        "results": [r.model_dump() for r in deduplicated],
        "results_by_theme": {
            theme: [r.model_dump() for r in items]
            for theme, items in processed_by_theme.items()
        },
        "result_count": len(deduplicated),
        "total_chunks": sum(len(r.chunks) for r in deduplicated),
        "theme_count": len(processed_by_theme),
    }

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_CONCEPT_SEARCH_POSTPROCESSING,
        service_type=SERVICE_TYPE_NONE,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "result_count_before": len(all_results),
            "result_count_after": len(deduplicated),
            "total_source_refs": ref_counter,
        },
    )

    out: dict = {
        "processed_concept_results": processed_concept_results,
        "source_references": source_references,
        "node_metrics": [metrics.model_dump()],
    }
    # When no chunks survive deduplication/filtering, mark no_data so the
    # router can exit cleanly and entity_runner records it as success (no_data).
    if len(deduplicated) == 0:
        out["pipeline_status"] = PIPELINE_STATUS_NO_DATA
    return out

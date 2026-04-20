"""
Node: concept_extraction

Uses an LLM to identify thematic concepts from the exploratory search chunks.
The extracted concepts (up to 5 categories × 3 concepts each) drive the
focused concept search in the next node.

Also populates ``themes`` — the ordered list of theme names used by the
bullet generation subgraph loop.

Service type: llm (single structured LLM call via BriefPipelineService.extract_concepts)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import NODE_CONCEPT_EXTRACTION, SERVICE_TYPE_LLM
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.models import ConceptExtraction, Entity, ReportDates, Result


def extract_thematic_concepts_from_chunks(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — concept_extraction.

    Deserializes ``exploratory_chunks`` from state, calls the LLM-backed
    concept extraction service, and stores the result as a serialized
    ``ConceptExtraction`` dict plus the ordered ``themes`` list.
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

    raw_chunks = state.get("exploratory_chunks") or []
    results: list[Result] = [Result.model_validate(r) for r in raw_chunks]

    concepts: ConceptExtraction = deps.brief_service.extract_concepts(
        entity=entity,
        report_dates=report_dates,
        results=results,
        request_id=state.get("request_id", ""),
        debug_logger=deps.debug_logger,
        entity_metrics=deps.entity_metrics,
    )

    themes = [cat.theme for cat in concepts.categories]

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_CONCEPT_EXTRACTION,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=1,
        extra={
            "total_categories": len(concepts.categories),
            "total_concepts": sum(len(c.concepts) for c in concepts.categories),
            "themes": themes,
        },
    )

    return {
        "extracted_concepts": concepts.model_dump(),
        "themes": themes,
        "node_metrics": [metrics.model_dump()],
    }

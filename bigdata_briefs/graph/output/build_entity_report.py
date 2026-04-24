"""
Node: build_report

Final node in the Brief pipeline.  Converts the accumulated ``bullet_points``
list into a ``SingleEntityReport`` and writes it to ``state.final_report``.

Constructs:
  - ``report_bulletpoints`` — active bullet texts (cleaned of `:ref[...]` markers)
  - ``bullet_citations`` — corresponding citation lists
  - ``relevance_score`` — per-bullet relevance scores (or threshold+1 as default)
  - ``clean_final_report`` — formatted markdown: ``* text :ref[...] \\n`` per bullet
  - ``entity_info`` — from ``entity.to_entity_info()``

Sets ``pipeline_status = "completed"``.

Service type: none (pure data assembly, no LLM or API calls)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.attribution.sources import format_bullet_with_citations
from bigdata_briefs.graph.constants import (
    NODE_BUILD_REPORT,
    PIPELINE_STATUS_COMPLETED,
    SERVICE_TYPE_NONE,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    NodeMetricsRecord,
    bullet_to_record,
)
from bigdata_briefs.models import Entity, SingleEntityReport
from bigdata_briefs.settings import settings


def assemble_single_entity_report(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — build_report.

    Assembles ``SingleEntityReport`` from the final ``bullet_points`` state
    and writes it to ``state.final_report`` as a dict.
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

    bullet_points: list[dict] = state.get("bullet_points") or []
    default_score = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE + 1

    report_bullets: list[str] = []
    bullet_citations: list[list[str]] = []
    relevance_scores: list[int] = []
    failed_bullets: list[dict] = []

    for bp in bullet_points:
        record = bullet_to_record(bp)
        if record.failure is not None:
            failed_bullets.append({
                "trace_id": record.trace_id,
                "text": record.text,
                "node_id": record.failure.node_id,
                "error_type": record.failure.error_type,
                "error_message": record.failure.error_message,
            })
            continue
        if not record.is_active:
            continue
        report_bullets.append(record.text)
        bullet_citations.append(record.citations)
        score = record.relevance_scoring.score if record.relevance_scoring else default_score
        relevance_scores.append(score)

    # Build clean_final_report markdown string
    clean_parts: list[str] = []
    for text, cits in zip(report_bullets, bullet_citations):
        formatted = format_bullet_with_citations(text, cits)
        clean_parts.append(f"* {formatted} \n")
    clean_final_report = "".join(clean_parts)

    entity_report = SingleEntityReport(
        entity_id=entity.id,
        entity_info=entity.to_entity_info().model_dump(exclude_none=True),
        clean_final_report=clean_final_report,
        report_bulletpoints=report_bullets,
        bullet_citations=bullet_citations,
        relevance_score=relevance_scores,
    )

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_BUILD_REPORT,
        service_type=SERVICE_TYPE_NONE,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={
            "active_bullets": len(report_bullets),
            "failed_bullets": len(failed_bullets),
            "failures": failed_bullets,
        },
    )

    return {
        "final_report": entity_report.model_dump_with_bullets(),
        "pipeline_status": PIPELINE_STATUS_COMPLETED,
        "node_metrics": [metrics.model_dump()],
    }

"""
Node: bullets_generation

For the current theme (identified by ``active_theme_index``), calls the LLM
to generate bullet points from the theme's search chunks. Each generated bullet
becomes a new ``BulletPointRecord`` with ``generation`` metadata populated.

Previously accumulated bullets for the current entity run are passed to the LLM
as an anti-duplication hint.

This node runs once per theme inside the ``Bullets_Generation_and_Scoring``
subgraph loop. The loop is driven by ``active_theme_index``.

Service type: llm (single structured LLM call per theme iteration)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.attribution.sources import (
    create_sources_for_results,
    merge_chunks_by_text,
    process_topic_collection_no_score,
    replace_references_in_topic_collection_no_score,
)
from bigdata_briefs.graph.constants import NODE_BULLETS_GENERATION, SERVICE_TYPE_LLM
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletPointRecord,
    GenerationMetadata,
    NodeMetricsRecord,
    record_to_bullet,
)
from bigdata_briefs.models import ConceptExtraction, Entity, ReportDates, Result, TopicCollectionNoScore
from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
from bigdata_briefs.prompts.user_prompts import get_iterative_theme_user_prompt
from bigdata_briefs.settings import settings


def produce_bullets_for_theme(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — bullets_generation.

    Runs the LLM-based bullet generation for the theme at ``active_theme_index``.
    Appends new ``BulletPointRecord`` dicts (with ``generation`` metadata only —
    relevance scoring is done by the next node) to ``bullet_points``.

    Does NOT run relevance scoring — that is the responsibility of the
    ``score_bullet_relevance`` node that follows immediately in the subgraph.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    theme_index: int = state.get("active_theme_index", 0)
    themes: list[str] = state.get("themes", [])

    if theme_index >= len(themes):
        # Safety: nothing to do
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_BULLETS_GENERATION,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "theme_index out of range"},
                ).model_dump()
            ]
        }

    current_theme = themes[theme_index]

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
    current_quarter_title = state.get("current_quarter_title") or None

    # Deserialize processed results
    processed = state.get("processed_concept_results") or {}
    results_by_theme_raw = processed.get("results_by_theme", {})
    results_by_theme: dict[str, list[Result]] = {
        theme: [Result.model_validate(r) for r in items]
        for theme, items in results_by_theme_raw.items()
    }
    # theme-specific chunks only (ITERATIVE_SEQUENTIAL_WITH_THEMATIC_CHUNKS mode)
    theme_chunks: list[Result] = results_by_theme.get(current_theme, [])

    # Build report_sources for this theme's chunks (required by attribution helpers)
    # We rebuild per-theme but re-use existing source_references so ref IDs stay stable
    report_sources, reverse_map = create_sources_for_results(theme_chunks)

    # Same-text deduplication (controlled by settings)
    theme_merged_chunks = None
    merged_ref_expansion = None
    if settings.DEDUPLICATE_SAME_TEXT and theme_chunks:
        theme_merged_chunks, merged_ref_expansion = merge_chunks_by_text(
            theme_chunks, report_sources
        )

    # Anti-duplication: pass previously accumulated bullets to the LLM
    existing_bullets: list[dict] = state.get("bullet_points") or []
    active_previous = [
        {"theme": bp["theme"], "text": bp["text"]}
        for bp in existing_bullets
        if bp.get("is_active", True) and bp.get("generation")
    ]

    # Concepts for this theme
    extracted = state.get("extracted_concepts") or {}
    concepts = ConceptExtraction.model_validate(extracted)
    theme_category = next(
        (cat for cat in concepts.categories if cat.theme == current_theme), None
    )
    theme_concepts = theme_category.concepts if theme_category else []
    other_themes = [t for t in themes if t != current_theme]

    prompt_keys = get_prompt_keys("entity_update_iterative_by_theme")
    user_prompt = get_iterative_theme_user_prompt(
        entity=entity,
        theme=current_theme,
        concepts=theme_concepts,
        other_themes=other_themes,
        results=theme_chunks,
        previous_bullets=active_previous,
        report_dates=report_dates,
        user_template=prompt_keys.user_template,
        response_format=f"{TopicCollectionNoScore.model_json_schema()}",
        report_sources=report_sources,
        merged_chunks=theme_merged_chunks,
        contextual_quarter=current_quarter_title,
    )

    messages = [
        {"role": "user", "content": user_prompt},
        {"role": "assistant", "content": "```json\n{"},
    ]

    collection: TopicCollectionNoScore = deps.llm_client.call_with_response_format(
        system=[{"role": "system", "content": prompt_keys.system_prompt}],
        messages=messages,
        text_format=TopicCollectionNoScore,
        step_name=f"bullets_generation_theme_{current_theme.replace(' ', '_')}",
        debug_logger=deps.debug_logger,
        entity_metrics=deps.entity_metrics,
        **prompt_keys.llm_kwargs,
    )

    updated_collection = replace_references_in_topic_collection_no_score(
        collection,
        reverse_map,
        entity,
        merged_ref_expansion=merged_ref_expansion if settings.DEDUPLICATE_SAME_TEXT else None,
    )
    topics, citations = process_topic_collection_no_score(updated_collection, report_sources)

    # Build BulletPointRecord objects with generation metadata only.
    # Relevance scoring will be added by score_bullet_relevance.
    timestamp_now = datetime.now(timezone.utc).isoformat()
    new_bullets = []
    for i, (text, cit) in enumerate(zip(topics, citations)):
        record = BulletPointRecord(
            trace_id=str(uuid4()),
            theme=current_theme,
            citations=list(cit) if cit else [],
            text=text,
            is_active=True,
            generation=GenerationMetadata(
                original_text=text,
                model=prompt_keys.llm_kwargs.get("model", ""),
                timestamp=timestamp_now,
                theme_index=theme_index,
                theme_name=current_theme,
            ),
        )
        new_bullets.append(record_to_bullet(record))

    # Merge with existing bullet_points
    updated_bullet_points = list(existing_bullets) + new_bullets

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_BULLETS_GENERATION,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=1,
        extra={
            "theme": current_theme,
            "theme_index": theme_index,
            "bullets_generated": len(new_bullets),
            "theme_chunks": len(theme_chunks),
        },
    )

    return {
        "bullet_points": updated_bullet_points,
        "node_metrics": [metrics.model_dump()],
    }

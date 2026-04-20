from jinja2 import Template

from bigdata_briefs.models import (
    ConceptExtraction,
    Entity,
    MergedChunkForPrompt,
    ReportDates,
    Result,
    RetrievedSources,
    SingleEntityReport,
)
from bigdata_briefs.templates import loader


def render_merged_results(merged_chunks: list[MergedChunkForPrompt]) -> str:
    """
    Render merged chunks for prompt using the merged template.
    
    Each MergedChunkForPrompt may have multiple headlines if the same text
    appeared in multiple news sources. The template displays them as
    "Title 1:", "Title 2:", etc.
    
    Args:
        merged_chunks: List of MergedChunkForPrompt objects
        
    Returns:
        Rendered markdown string for the prompt
    """
    return (
        loader.get_template("prompts/results_merged.md.jinja")
        .render(merged_chunks=merged_chunks)
        .strip()
    )


def get_concept_extraction_user_prompt(
    *,
    entity: Entity,
    results: list[Result],
    report_dates: ReportDates,
    response_format: str,
    user_template: Template,
) -> str:
    """Generate user prompt for extracting concepts from search results."""
    results_md = (
        loader.get_template("prompts/results.md.jinja").render(results=results).strip()
    )

    return user_template.render(
        entity_info=entity,
        results_md=results_md,
        start_date=report_dates.get_start_date_formatted(),
        end_date=report_dates.get_end_date_formatted(),
        date_instructions=report_dates.get_date_filter_instructions(),
        response_format=response_format,
        current_datetime=report_dates.get_current_date_for_prompt(),
    )


def get_report_from_concepts_user_prompt(
    *,
    entity: Entity,
    concepts: ConceptExtraction,
    results: list[Result],
    report_dates: ReportDates,
    user_template: Template,
    response_format: str,
    report_sources: RetrievedSources | None,
):
    """Generate user prompt for creating a report based on extracted concepts."""
    # Render results with references if available
    if report_sources:
        rendered_results = (
            loader.get_template("prompts/results_with_refs.md.jinja")
            .render(results=results, report_sources=report_sources.root)
            .strip()
        )
    else:
        rendered_results = (
            loader.get_template("prompts/results.md.jinja").render(results=results).strip()
        )

    entity_info = f"{entity.name} ({entity.ticker})" if entity.ticker else entity.name

    # Render concepts as markdown
    concepts_lines = []
    for category in concepts.categories:
        concepts_lines.append(f"**{category.theme}**: {', '.join(category.concepts)}")
    concepts_md = "\n".join(concepts_lines)

    return user_template.render(
        entity_info=entity_info,
        concepts_md=concepts_md,
        rendered_results=rendered_results,
        start_date=report_dates.get_start_date_formatted(),
        end_date=report_dates.get_end_date_formatted(),
        date_instructions=report_dates.get_date_filter_instructions(),
        current_datetime=report_dates.get_current_date_for_prompt(),
        response_format=response_format,
    )


def get_iterative_theme_user_prompt(
    *,
    entity: Entity,
    theme: str,
    concepts: list[str],
    other_themes: list[str],
    results: list[Result],
    previous_bullets: list[dict],  # List of {"theme": str, "text": str}
    report_dates: ReportDates,
    user_template: Template,
    response_format: str,
    report_sources: RetrievedSources | None,
    merged_chunks: list[MergedChunkForPrompt] | None = None,
    contextual_quarter: str | None = None,
):
    """Generate user prompt for iterative theme processing.
    
    This prompt is used when processing themes sequentially, passing all chunks
    but focusing on one theme at a time, with explicit exclusion of other themes
    and awareness of previously generated bullets to avoid duplication.
    
    Args:
        entity: The entity being analyzed
        theme: Current theme to focus on
        concepts: Concepts for the current theme (as example topics)
        other_themes: Other themes to explicitly exclude
        results: All results (ordered with current theme's chunks LAST)
        previous_bullets: Previously generated bullets with their themes
        report_dates: Date range for the report
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
        report_sources: Source mapping for reference IDs
        merged_chunks: Optional list of merged chunks (when DEDUPLICATE_SAME_TEXT is enabled)
    """
    # If merged_chunks provided, use merged template; otherwise use standard templates
    if merged_chunks is not None:
        rendered_results = render_merged_results(merged_chunks)
    elif report_sources:
        rendered_results = (
            loader.get_template("prompts/results_with_refs.md.jinja")
            .render(results=results, report_sources=report_sources.root)
            .strip()
        )
    else:
        rendered_results = (
            loader.get_template("prompts/results.md.jinja").render(results=results).strip()
        )

    entity_info = f"{entity.name} ({entity.ticker})" if entity.ticker else entity.name

    # Format concepts as comma-separated list
    concepts_str = ", ".join(concepts)

    return user_template.render(
        entity_info=entity_info,
        theme=theme,
        concepts=concepts_str,
        other_themes=other_themes,
        previous_bullets=previous_bullets,
        rendered_results=rendered_results,
        start_date=report_dates.get_start_date_formatted(),
        end_date=report_dates.get_end_date_formatted(),
        date_instructions=report_dates.get_date_filter_instructions(),
        current_datetime=report_dates.get_current_date_for_prompt(),
        contextual_quarter=contextual_quarter or "N/A",
        response_format=response_format,
    )


def get_thematic_clustering_user_prompt(
    *,
    entity_name: str,
    bullets: list[dict],  # List of {"text": str, "citations": list}
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for thematic clustering of bullet points.
    
    Args:
        entity_name: Name of the entity
        bullets: List of bullet points with text and citations
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        bullets=bullets,
        response_format=response_format,
    )


def get_consolidate_theme_user_prompt(
    *,
    entity_name: str,
    rationale: str,
    group_bullets: list[dict],  # List of {"text": str, "citations": str}
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for consolidating a group of bullet points.
    
    Args:
        entity_name: Name of the entity
        rationale: Reason why these bullets should be consolidated
        group_bullets: List of bullet points to consolidate
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        rationale=rationale,
        group_bullets=group_bullets,
        response_format=response_format,
    )


def get_standalone_analyze_user_prompt(
    *,
    entity_name: str,
    consolidated_bullets: list[str],
    standalone_bullets: list[str],
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for analyzing standalone bullet points.
    
    Args:
        entity_name: Name of the entity
        consolidated_bullets: List of already consolidated bullet texts
        standalone_bullets: List of standalone bullet texts
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        consolidated_bullets=consolidated_bullets,
        standalone_bullets=standalone_bullets,
        response_format=response_format,
    )


def get_standalone_merge_user_prompt(
    *,
    entity_name: str,
    bullets_to_merge: list[str],
    rationale: str,
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for merging standalone bullet points.
    
    Args:
        entity_name: Name of the entity
        bullets_to_merge: List of bullet points to merge together
        rationale: Reason for merging
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        bullets_to_merge=bullets_to_merge,
        rationale=rationale,
        response_format=response_format,
    )


def get_standalone_rewrite_user_prompt(
    *,
    entity_name: str,
    original_bullet: str,
    rationale: str,
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for rewriting a standalone bullet point.
    
    Args:
        entity_name: Name of the entity
        original_bullet: The bullet point to rewrite
        rationale: Explains what is redundant and what is unique to keep
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        original_bullet=original_bullet,
        rationale=rationale,
        response_format=response_format,
    )


# ===== STEP 8: REDUNDANCY VALIDATION PROMPTS =====

def get_redundancy_identify_user_prompt(
    *,
    entity_name: str,
    bullets: list[str],
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for identifying redundant bullet points.
    
    Args:
        entity_name: Name of the entity
        bullets: List of bullet point texts to check for duplicates
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        bullets=bullets,
        response_format=response_format,
    )


def get_redundancy_merge_user_prompt(
    *,
    entity_name: str,
    bullets_to_merge: list[str],
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for merging redundant bullet points.
    
    Args:
        entity_name: Name of the entity
        bullets_to_merge: List of bullet points to merge together
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        bullets_to_merge=bullets_to_merge,
        response_format=response_format,
    )


def get_redundancy_rewrite_user_prompt(
    *,
    entity_name: str,
    original_bullet: str,
    rationale: str,
    user_template: Template,
    response_format: str,
):
    """Generate user prompt for rewriting a bullet to remove redundant information.
    
    Args:
        entity_name: Name of the entity
        original_bullet: The bullet point to rewrite
        rationale: Explains what is duplicate and what is unique to keep
        user_template: Jinja template for the prompt
        response_format: JSON schema for response
    """
    return user_template.render(
        entity_name=entity_name,
        original_bullet=original_bullet,
        rationale=rationale,
        response_format=response_format,
    )

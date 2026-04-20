"""In-memory bullet pipeline checkpoint and eligibility gates (novelty phase)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class BulletPipelineCheckpoint(BaseModel):
    """Per-bullet state from generation through novelty-via-search (LangGraph)."""

    model_config = ConfigDict(extra="ignore")

    bullet_trace_id: UUID = Field(default_factory=uuid4)
    entity_id: str = ""
    report_date: datetime | None = None
    checkpoint_saved_at: datetime | None = None

    text_after_generation: str = ""
    post_generation_entity_relevance_pass: bool | None = None
    post_generation_relevance_score: int | None = None

    entity_grounding_pass: bool | None = None
    text_after_entity_grounding: str | None = None

    novelty_embedding_final_decision: str | None = None
    novelty_embedding_combined_reason: str | None = None
    novelty_embedding_evaluator_details: list[dict] | None = None
    text_after_novelty_embedding_rewrite: str | None = None
    novelty_embedding_rewrite_relevance_checked: bool | None = None
    novelty_embedding_rewrite_relevance_pass: bool | None = None
    novelty_embedding_rewrite_relevance_score: int | None = None
    novelty_embedding_completed: bool = False

    novelty_search_graph_verdict: str | None = None
    novelty_search_graph_rewritten_text: str | None = None
    novelty_search_graph_duration_seconds: float | None = None
    novelty_search_rewrite_relevance_checked: bool | None = None
    novelty_search_rewrite_relevance_pass: bool | None = None
    novelty_search_rewrite_relevance_score: int | None = None
    text_after_novelty_search: str | None = None

    bullet_citations: list[str] = Field(default_factory=list)


def _post_generation_relevance_for_lists(cp: BulletPipelineCheckpoint) -> int:
    """Score from the post-generation relevance LLM check; default when unset."""
    if cp.post_generation_relevance_score is not None:
        return cp.post_generation_relevance_score
    return 4


def is_eligible_for_novelty_embedding(cp: BulletPipelineCheckpoint) -> bool:
    """True if this bullet may enter the LLM novelty phase (after generation + grounding gates)."""
    if cp.post_generation_entity_relevance_pass is not True:
        return False
    if cp.entity_grounding_pass is not True:
        return False
    return True


def is_eligible_for_novelty_search(cp: BulletPipelineCheckpoint) -> bool:
    """True if this bullet may enter novelty-via-search (LangGraph)."""
    if not cp.novelty_embedding_completed:
        return False
    decision = (cp.novelty_embedding_final_decision or "").lower()
    if decision not in ("keep", "rewrite"):
        return False
    return is_eligible_for_novelty_embedding(cp)


def checkpoints_eligible_for_embedding(
    checkpoints: list[BulletPipelineCheckpoint],
) -> list[BulletPipelineCheckpoint]:
    """Stable order: same as ``checkpoints`` iteration."""
    return [c for c in checkpoints if is_eligible_for_novelty_embedding(c)]


def checkpoints_eligible_for_search(
    checkpoints: list[BulletPipelineCheckpoint],
) -> list[BulletPipelineCheckpoint]:
    return [c for c in checkpoints if is_eligible_for_novelty_search(c)]


def checkpoints_passed_generation_relevance(
    checkpoints: list[BulletPipelineCheckpoint],
) -> list[BulletPipelineCheckpoint]:
    """Checkpoints that passed the post-generation relevance gate (entity grounding input)."""
    return [c for c in checkpoints if c.post_generation_entity_relevance_pass is True]


def grounding_validator_inputs_from_checkpoints(
    checkpoints: list[BulletPipelineCheckpoint],
) -> tuple[list[str], list[list[str]], list[int]]:
    """Parallel (bullets, citations, scores) for ``EntityGroundingValidator`` from checkpoints."""
    cps = checkpoints_passed_generation_relevance(checkpoints)
    bullets = [c.text_after_generation for c in cps]
    cites = [list(c.bullet_citations) for c in cps]
    scores = [_post_generation_relevance_for_lists(c) for c in cps]
    return bullets, cites, scores


def has_grounding_survivors(checkpoints: list[BulletPipelineCheckpoint]) -> bool:
    """True if at least one checkpoint passed entity grounding."""
    return any(c.entity_grounding_pass is True for c in checkpoints)


def sync_report_lists_after_grounding(
    checkpoints: list[BulletPipelineCheckpoint],
) -> tuple[list[str], list[list[str]], list[int]]:
    """
    Build report lists from checkpoints when novelty is skipped: survivors after grounding only.

    Order follows ``checkpoints`` iteration.
    """
    texts: list[str] = []
    cites: list[list[str]] = []
    scores: list[int] = []
    for cp in checkpoints:
        if cp.post_generation_entity_relevance_pass is not True:
            continue
        if cp.entity_grounding_pass is not True:
            continue
        t = cp.text_after_entity_grounding or cp.text_after_generation
        texts.append(t)
        cites.append(list(cp.bullet_citations))
        scores.append(_post_generation_relevance_for_lists(cp))
    return texts, cites, scores


def sync_report_lists_from_checkpoints(
    checkpoints: list[BulletPipelineCheckpoint],
) -> tuple[list[str], list[list[str]], list[int]]:
    """
    Build ``report_bulletpoints``, parallel citations (as list-of-lists), relevance scores
    from checkpoints that completed LLM novelty with a kept decision (KEEP/REWRITE).
    Order follows ``checkpoints`` list order.
    """
    texts: list[str] = []
    cites: list[list[str]] = []
    scores: list[int] = []
    for cp in checkpoints:
        if not cp.novelty_embedding_completed:
            continue
        decision = (cp.novelty_embedding_final_decision or "").lower()
        if decision not in ("keep", "rewrite"):
            continue
        t = (
            cp.text_after_novelty_search
            if cp.text_after_novelty_search is not None
            and cp.text_after_novelty_search != ""
            else (
                cp.text_after_novelty_embedding_rewrite
                or cp.text_after_entity_grounding
                or cp.text_after_generation
            )
        )
        texts.append(t)
        cites.append(list(cp.bullet_citations))
        scores.append(_post_generation_relevance_for_lists(cp))
    return texts, cites, scores

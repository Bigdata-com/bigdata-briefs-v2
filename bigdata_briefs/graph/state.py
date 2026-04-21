"""
LangGraph state definition for the Brief 2.0 pipeline.

BriefGraphState is the single TypedDict that flows through all nodes.
BulletPointRecord and its nested sub-models define the structured per-bullet
metadata that accumulates as each node processes bullets.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Bullet sub-models: each node writes ONLY its own block ───────────────────


class GenerationMetadata(BaseModel):
    """Written by: bullets_generation node."""

    original_text: str
    model: str = ""
    timestamp: str = ""          # ISO 8601
    theme_index: int = 0
    theme_name: str = ""


class RelevanceScoringMetadata(BaseModel):
    """Written by: relevance_score node."""

    score: int                   # 1-5
    reason: str = ""
    passed: bool                 # score > INTRO_SECTION_MIN_RELEVANCE_SCORE


class GroundingCheckMetadata(BaseModel):
    """Written by: entity_grounding_check node."""

    decision: Literal["valid", "invalid"]
    reason: str = ""


class EntityGroundingBlock(BaseModel):
    """Written by: entity_grounding_check node."""

    check: GroundingCheckMetadata | None = None


class EmbeddingJudgmentMetadata(BaseModel):
    """Written by: novelty_judgment_embedding node."""

    decision: Literal["keep", "discard", "rewrite"]
    reason: str = ""
    evaluator_details: list[dict] = Field(default_factory=list)


class EmbeddingRewriteMetadata(BaseModel):
    """Written by: rewrite_embedding node."""

    text_before: str
    text_after: str
    is_empty: bool = False


class EmbeddingRelevanceMetadata(BaseModel):
    """Written by: relevance_check_embedding node."""

    score: int
    passed: bool


class NoveltyEmbeddingBlock(BaseModel):
    """Written by: novelty embedding phase nodes (9a-9d)."""

    judgment: EmbeddingJudgmentMetadata | None = None
    rewrite: EmbeddingRewriteMetadata | None = None
    relevance_check: EmbeddingRelevanceMetadata | None = None


class SearchNoveltyMetadata(BaseModel):
    """Written by: novelty_via_search node."""

    verdict: Literal["keep", "discard", "rewrite"]
    rewritten_text: str | None = None
    duration_seconds: float = 0.0
    # Human-readable explanation from the external novelty-via-search subgraph,
    # e.g. "Bullet closely matches article from 2026-04-14 about IBIT inflows".
    reason: str | None = None
    # Full raw output dict from the external subgraph (minus large fields),
    # preserved for debugging and the discarded-bullets trace endpoint.
    details: dict | None = None
    # Aggregate novelty verdict across all claims.
    # novel            — all claims fully novel; published as-is
    # mixed            — novel + old/partially_novel context; rewriter restructures with old clause + pivot marker
    # mixed_noise      — novel + only trivial/unsupported noise; rewriter strips noise, keeps novel text
    # mixed_weak       — only partially_novel claims; discarded
    # discard_not_new  — all claims old or trivial; discarded
    # discard_unsupported — at least one unsupported inference; discarded
    # Populated by rewrite_search_bullets; used by save_novel_bullets to flag
    # not_fully_novel bullets (overall_verdict == "mixed" and not discarded).
    overall_verdict: Literal[
        "novel", "mixed", "mixed_noise", "mixed_weak", "discard_not_new", "discard_unsupported", "old"
    ] | None = None


class SearchRelevanceMetadata(BaseModel):
    """Written by: relevance_score_search node."""

    score: int
    passed: bool
    reasoning: str | None = None  # LLM justification for the score


class NoveltySearchBlock(BaseModel):
    """Written by: novelty search phase nodes (10a-10b)."""

    search: SearchNoveltyMetadata | None = None
    relevance_check: SearchRelevanceMetadata | None = None


class BulletFailure(BaseModel):
    """Written when a node raises an unhandled exception for a specific bullet."""

    node_id: str
    error_type: str    # exception class name
    error_message: str


class BulletPointRecord(BaseModel):
    """
    A single bullet point and all metadata accumulated during pipeline processing.

    Invariants:
    - Each node writes ONLY its own nested key.
    - ``text`` is always the latest version of the bullet text.
    - ``is_active = False`` means the bullet was discarded; downstream nodes skip it.
    - ``citations`` tracks the current valid set of reference IDs.
    - ``failure`` is set when a node raises an exception while processing this bullet;
      the bullet is deactivated and the failure is preserved for post-run inspection.
    """

    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    theme: str = ""
    citations: list[str] = Field(default_factory=list)   # current valid ref IDs
    text: str = ""                                         # always the latest version
    is_active: bool = True                                 # False = discarded

    # Stage-specific blocks — each node writes ONLY its own key
    generation: GenerationMetadata | None = None
    relevance_scoring: RelevanceScoringMetadata | None = None
    entity_grounding: EntityGroundingBlock | None = None
    novelty_embedding: NoveltyEmbeddingBlock | None = None
    novelty_search: NoveltySearchBlock | None = None
    failure: BulletFailure | None = None


# ── Metrics record ────────────────────────────────────────────────────────────


class NodeMetricsRecord(BaseModel):
    """Per-node execution metrics. Appended to state.node_metrics."""

    node_id: str
    service_type: Literal["llm", "search", "embed", "none"]
    started_at: str                  # ISO 8601
    ended_at: str                    # ISO 8601
    wall_time_ms: float
    llm_cost_usd: float = 0.0
    llm_tokens: int = 0
    llm_calls: int = 0
    search_cost_usd: float = 0.0
    search_calls: int = 0
    embedding_cost_usd: float = 0.0
    embedding_tokens: int = 0
    invocation_index: int = 0
    error_count: int = 0
    extra: dict = Field(default_factory=dict)


# ── LangGraph State ───────────────────────────────────────────────────────────


class BriefGraphState(TypedDict):
    """
    The single state object that flows through every node in the Brief pipeline.

    Design decisions:
    - ``bullet_points`` has NO reducer — nodes return the full updated list.
    - Only ``node_metrics`` and ``debug_events`` use operator.add (append-only).
    - Identity fields (entity_*, dates, request_id) are set at invocation and never mutated.
    """

    # ── Identity (set at invocation, never mutated) ───────────────────────────
    entity_id: str
    entity_name: str
    entity_type: str
    entity_ticker: str
    report_start_date: str              # ISO 8601
    report_end_date: str                # ISO 8601
    request_id: str                     # UUID string
    config: dict                        # source_filter, categories, flags, etc.

    # ── Phase 1 outputs ───────────────────────────────────────────────────────
    initial_check_result: dict          # {has_results: bool, result_count: int}
    exploratory_chunks: list[dict]      # serialized Result objects
    current_quarter_title: str          # "" or "Q1 2026" etc.
    extracted_concepts: dict            # serialized ConceptExtraction
    raw_concept_results: dict           # {all_results, results_per_concept, results_by_theme}
    processed_concept_results: dict     # post-dedup/rerank {results, results_by_theme}

    # ── Core bullet tracking (Phase 2 onward) ────────────────────────────────
    bullet_points: list[dict]           # list of BulletPointRecord dicts
    source_references: dict             # ref_id -> SourceChunkReference dict

    # ── Control flow ─────────────────────────────────────────────────────────
    pipeline_status: str                # "running" | "completed" | "no_data" | "error"
    active_theme_index: int             # current theme index for subgraph loop
    themes: list[str]                   # ordered theme names from concept extraction

    # ── Append-only (LangGraph reducers) ─────────────────────────────────────
    node_metrics: Annotated[list[dict], operator.add]   # NodeMetricsRecord dicts
    debug_events: Annotated[list[dict], operator.add]   # optional debug payloads

    # ── Final output ──────────────────────────────────────────────────────────
    final_report: dict                  # SingleEntityReport.model_dump() or None


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_active_bullets(bullet_points: list[dict]) -> list[dict]:
    """Return only bullets where is_active is True."""
    return [bp for bp in bullet_points if bp.get("is_active", True)]


def bullet_to_record(bp: dict) -> BulletPointRecord:
    """Deserialize a bullet dict from state into a BulletPointRecord."""
    return BulletPointRecord.model_validate(bp)


def record_to_bullet(record: BulletPointRecord) -> dict:
    """Serialize a BulletPointRecord to a plain dict for state storage."""
    return record.model_dump()


def make_empty_state_defaults() -> dict:
    """Return default values for optional state fields (for partial invocations)."""
    return {
        "initial_check_result": {},
        "exploratory_chunks": [],
        "current_quarter_title": "",
        "extracted_concepts": {},
        "raw_concept_results": {},
        "processed_concept_results": {},
        "bullet_points": [],
        "source_references": {},
        "pipeline_status": "running",
        "active_theme_index": 0,
        "themes": [],
        "node_metrics": [],
        "debug_events": [],
        "final_report": {},
    }

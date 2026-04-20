"""SQLModel table for bullet pipeline checkpoints (same SQLite as embeddings)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class SQLBulletPipelineCheckpoint(SQLModel, table=True):
    """Persisted audit row for one bullet trace through the novelty phase."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    bullet_trace_id: uuid.UUID = Field(index=True)
    entity_id: str = Field(index=True)
    report_date: datetime | None = None
    checkpoint_saved_at: datetime | None = None

    text_after_generation: str = ""
    post_generation_entity_relevance_pass: bool | None = None
    post_generation_relevance_score: int | None = None

    entity_grounding_pass: bool | None = None
    text_after_entity_grounding: str | None = None

    novelty_embedding_final_decision: str | None = None
    novelty_embedding_combined_reason: str | None = None
    novelty_embedding_evaluator_details: list | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
    )
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

    bullet_citations: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))

"""Persist ``BulletPipelineCheckpoint`` rows to SQLite."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.engine import Engine
from sqlmodel import Session

from bigdata_briefs.novelty.bullet_pipeline_checkpoint import BulletPipelineCheckpoint
from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint


def checkpoint_to_sql_row(
    cp: BulletPipelineCheckpoint,
    *,
    saved_at: datetime | None = None,
) -> SQLBulletPipelineCheckpoint:
    """Map Pydantic checkpoint to SQLModel row."""
    ts = saved_at or datetime.now(timezone.utc)
    return SQLBulletPipelineCheckpoint(
        bullet_trace_id=cp.bullet_trace_id,
        entity_id=cp.entity_id,
        report_date=cp.report_date,
        checkpoint_saved_at=ts,
        text_after_generation=cp.text_after_generation,
        post_generation_entity_relevance_pass=cp.post_generation_entity_relevance_pass,
        post_generation_relevance_score=cp.post_generation_relevance_score,
        entity_grounding_pass=cp.entity_grounding_pass,
        text_after_entity_grounding=cp.text_after_entity_grounding,
        novelty_embedding_final_decision=cp.novelty_embedding_final_decision,
        novelty_embedding_combined_reason=cp.novelty_embedding_combined_reason,
        novelty_embedding_evaluator_details=cp.novelty_embedding_evaluator_details,
        text_after_novelty_embedding_rewrite=cp.text_after_novelty_embedding_rewrite,
        novelty_embedding_rewrite_relevance_checked=cp.novelty_embedding_rewrite_relevance_checked,
        novelty_embedding_rewrite_relevance_pass=cp.novelty_embedding_rewrite_relevance_pass,
        novelty_embedding_rewrite_relevance_score=cp.novelty_embedding_rewrite_relevance_score,
        novelty_embedding_completed=cp.novelty_embedding_completed,
        novelty_search_graph_verdict=cp.novelty_search_graph_verdict,
        novelty_search_graph_rewritten_text=cp.novelty_search_graph_rewritten_text,
        novelty_search_graph_duration_seconds=cp.novelty_search_graph_duration_seconds,
        novelty_search_rewrite_relevance_checked=cp.novelty_search_rewrite_relevance_checked,
        novelty_search_rewrite_relevance_pass=cp.novelty_search_rewrite_relevance_pass,
        novelty_search_rewrite_relevance_score=cp.novelty_search_rewrite_relevance_score,
        text_after_novelty_search=cp.text_after_novelty_search,
        bullet_citations=list(cp.bullet_citations) if cp.bullet_citations else None,
    )


class PipelineCheckpointStorage:
    """Write pipeline checkpoints in one transaction."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def store_all(
        self,
        checkpoints: list[BulletPipelineCheckpoint],
        *,
        saved_at: datetime | None = None,
    ) -> None:
        if not checkpoints:
            return
        rows = [checkpoint_to_sql_row(c, saved_at=saved_at) for c in checkpoints]
        with Session(self.engine) as session:
            for row in rows:
                session.add(row)
            session.commit()

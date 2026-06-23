from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BulletPointEmbedding(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    date: datetime
    entity_id: str
    embedding: list[float] | None = None
    original_text: str
    # For rewrite bullets: the text before rewriting. NULL for keep/discard.
    pre_rewrite_text: str | None = None
    status: str | None = None  # keep | discard_by_novelty | discard_by_relevance | rewrite
    # True = novel (keep or rewrite), False = not novel; None = legacy
    novelty: bool | None = None
    evaluator_details: list[dict] | None = None
    # Contextual quarter for this report date (e.g. "Q1 2026") from earnings calendar
    earnings_call_date: str | None = None
    added_past_evidence_from: str | None = None
    status_novelty_check_bigdata: str | None = None
    status_embedding: bool | None = None
    report_window_start: datetime | None = None
    report_window_end: datetime | None = None
    _is_fully_novel: bool = True

    def is_fully_novel(self):
        if self.novelty is not None:
            return self.novelty
        return self._is_fully_novel

    def set_novel(self, value: bool):
        self._is_fully_novel = value
        self.novelty = value


class CitationDetail(BaseModel):
    """A resolved citation: source ID + headline (title) + chunk text."""
    id: str           # e.g. "CQS:REF0"
    headline: str     # article / document title
    text: str         # chunk text used as evidence
    url: str | None = None  # original article URL
    source_name: str = ""  # publisher name (e.g. "Benzinga", "MT Newswires")


class GeneratedBulletPoint(BaseModel):
    """
    Record of a bullet point that passed all novelty/relevance checks and was
    included in the final report. Stored in the `generated_bullet_points` table
    (no embedding — links to `sqlbulletpointembedding` via `trace_id`).
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Run-level
    run_id: str
    entity_id: str
    entity_name: str
    report_window_start: datetime
    report_window_end: datetime
    created_at: datetime

    # Bullet-level
    trace_id: str
    text: str
    citations: list[CitationDetail] | None = None
    embedding_decision: str | None = None  # "keep" | "rewrite" | "discard"
    search_action: str | None = None       # "keep" | "rewrite" | "discard" | None
    # False when novelty_search verdict=="keep" but overall_verdict=="mixed":
    # the bullet passed but at least one of its claims was already known.
    is_fully_novel: bool = True


@dataclass
class ChunkTextHash:
    """
    Represents a chunk text hash for storage and retrieval.
    
    Used to track which chunk texts have been used in previous runs,
    enabling filtering of already-seen content.
    """
    entity_id: str  # Entity this chunk was used for
    date: datetime  # When the chunk was used (report date)
    text_hash: str  # SHA256 hash of the chunk text
    chunk_key: str  # Original "doc_id-chunk_num" for reference

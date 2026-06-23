import uuid
from datetime import datetime

from sqlalchemy import Boolean
from sqlmodel import JSON, Column, Field, SQLModel


class SQLBulletPointEmbedding(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entity_id: str
    date: datetime
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    original_text: str
    # For rewrite bullets: the text before rewriting. NULL for keep/discard.
    pre_rewrite_text: str | None = Field(default=None, nullable=True)
    # keep | discard_by_novelty | discard_by_relevance | rewrite; NULL = legacy
    status: str | None = None
    # True = novel (keep or rewrite), False = not novel (discard). Used for retrieval; NULL = legacy.
    novelty: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    # List of evaluator dicts (evaluator_name, decision, reason, rewritten_text, retrieved_bullets)
    evaluator_details: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Contextual quarter for this report date (e.g. "Q1 2026") from earnings calendar; NULL = legacy/unknown.
    # For existing DBs without this column: ALTER TABLE sqlbulletpointembedding ADD COLUMN earnings_call_date TEXT;
    earnings_call_date: str | None = Field(default=None, nullable=True)
    # Novelty-search archive role: rewrite | discard | canonical; NULL = not from novelty-search archive.
    added_past_evidence_from: str | None = Field(default=None, nullable=True)
    # Novelty-via-search overall_verdict: novel | mixed | old; NULL = not run / legacy.
    status_novelty_check_bigdata: str | None = Field(default=None, nullable=True)
    # True when a vector is stored; NULL = legacy row before backfill (treat like embedding present in retrieve).
    status_embedding: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    # Pipeline ReportDates [start, end) when this row was written; NULL = legacy.
    report_window_start: datetime | None = Field(default=None, nullable=True)
    report_window_end: datetime | None = Field(default=None, nullable=True)


class SQLGeneratedBulletPoint(SQLModel, table=True):
    """
    Stores the bullet points that were included in the final report for each run.
    No embeddings here — those live in `sqlbulletpointembedding` and are linked
    via `trace_id`.
    """
    __tablename__ = "generated_bullet_points"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

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
    citations: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    embedding_decision: str | None = Field(default=None, nullable=True)
    search_action: str | None = Field(default=None, nullable=True)
    # False when search verdict=="keep" but overall claim novelty is "mixed".
    is_fully_novel: bool = Field(default=True)


class SQLChunkTextHash(SQLModel, table=True):
    """
    Stores SHA256 hashes of chunk texts to detect already-used content across runs.
    
    When a chunk's text hash is found in the database for the same entity within
    the lookback period, it means that chunk was already used in a previous run
    and should be filtered out.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entity_id: str  # Entity this chunk was used for
    date: datetime  # When the chunk was used (report date)
    text_hash: str  # SHA256 hash of the chunk text (64 chars)
    chunk_key: str  # Original "doc_id-chunk_num" for reference/debugging

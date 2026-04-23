"""SQLModel tables for entity run orchestration (cursor, KG cache, run audit)."""

from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import Text
from sqlmodel import Field, SQLModel


class SQLBatchParallelRun(SQLModel, table=True):
    """One row per batch submitted via POST /batch/run-parallel."""

    batch_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    submitted_at: datetime
    total: int
    entity_ids_json: str = Field(sa_type=Text)   # JSON list of entity_id strings
    run_ids_json: str = Field(sa_type=Text)        # JSON dict {entity_id: str(run_id)}


class SQLEntityOrchestrationState(SQLModel, table=True):
    """One row per entity: incremental window cursor + denormalized KG cache."""

    entity_id: str = Field(primary_key=True, max_length=64)
    last_window_end: datetime | None = Field(default=None, nullable=True)
    kg_name: str | None = Field(default=None, nullable=True)
    kg_category: str | None = Field(default=None, nullable=True)
    kg_ticker: str | None = Field(default=None, nullable=True)
    kg_payload_json: str | None = Field(default=None, sa_type=Text, nullable=True)
    kg_fetched_at: datetime | None = Field(default=None, nullable=True)
    updated_at: datetime | None = Field(default=None, nullable=True)


class SQLEntityPipelineRunLog(SQLModel, table=True):
    """Append-style audit + single-flight lease per entity."""

    run_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entity_id: str = Field(index=True, max_length=64)
    report_window_start: datetime
    report_window_end: datetime
    process_started_at_utc: datetime
    process_completed_at_utc: datetime | None = Field(default=None, nullable=True)
    status: str = Field(max_length=32)  # running | succeeded | failed
    error_summary: str | None = Field(default=None, sa_type=Text, nullable=True)
    exit_code: int | None = Field(default=None, nullable=True)
    output_json: str | None = Field(default=None, sa_type=Text, nullable=True)

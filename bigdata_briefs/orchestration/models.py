"""SQLModel tables for entity run orchestration (cursor, KG cache, run audit)."""

from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import Text
from sqlmodel import Field, SQLModel


class SQLUIScanRun(SQLModel, table=True):
    """Tracks a day-by-day historical scan for a single entity.

    A scan takes an entity + start date and runs one pipeline window per day
    (00:00:00 → 23:59:59) sequentially until today (or an explicit end date).
    If the entity already has runs the scan resumes from the last window end,
    skipping days that were already covered.
    """

    scan_id: str = Field(primary_key=True, max_length=36)
    entity_id: str = Field(max_length=64)
    entity_name: str = Field(max_length=256)
    status: str = Field(max_length=32)   # running | finished | cancelled
    windows_total: int
    windows_done: int = Field(default=0)
    results_json: str = Field(default="[]", sa_type=Text)  # list of per-window result dicts
    created_at: datetime
    updated_at: datetime


class SQLUIBatchRun(SQLModel, table=True):
    """
    Persists the state of a UI batch run to SQLite.

    WHY: previously the batch state (progress, results, cancel flag) lived only in
    app.state.active_batches — a plain dict in RAM. This required Fly.io machines to
    stay alive 24/7 (auto_stop_machines=false) costing ~$10/month idle. If the machine
    restarted between two HTMX polls (every 3 s) the browser would get a 404 and the
    results would be lost.

    With this table the background thread writes each entity result to DB as it
    completes, and the polling route reads from DB instead of RAM. The machine can now
    safely stop and restart between polls without losing anything: SQLite lives on the
    persistent Fly.io volume at /data, which survives machine restarts.

    Status lifecycle: running → finished | cancelled
    """

    # UUID assigned at batch creation and returned to the browser as batch_id
    batch_id: str = Field(primary_key=True, max_length=36)

    # running | finished | cancelled
    # The background thread checks this at the start of each entity: if it reads
    # "cancelled" it skips remaining entities and marks the batch finished.
    status: str = Field(max_length=32)

    # JSON list of entity_id strings submitted for this batch
    entity_ids_json: str = Field(sa_type=Text)

    # JSON list of EntityRunStatus dicts — appended after each entity completes.
    # The polling route deserialises this to render the progress / results HTML.
    results_json: str = Field(default="[]", sa_type=Text)

    total: int
    done: int = Field(default=0)

    created_at: datetime
    updated_at: datetime


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

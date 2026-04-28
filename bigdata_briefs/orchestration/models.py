"""SQLModel tables for entity run orchestration (cursor, KG cache, run audit)."""

from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import Text
from sqlmodel import Field, SQLModel


class SQLBulletRunLog(SQLModel, table=True):
    """One row per bullet per pipeline run — structured metadata from output_json.

    output_json stores the entire BulletPointRecord trace as raw JSON (can be
    several MB per run). Parsing it at query time to show the Details page is
    too expensive. This table denormalises the fields that matter for analysis
    into typed columns written once, at the end of each run.

    All stage fields are nullable: a bullet that was discarded at relevance
    scoring never reaches novelty, so those columns stay NULL.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # ── provenance ──────────────────────────────────────────────────────────
    run_id: uuid.UUID = Field(index=True)   # FK → SQLEntityPipelineRunLog.run_id
    entity_id: str = Field(index=True, max_length=64)
    trace_id: str = Field(index=True, max_length=64)  # unique per bullet

    # ── final outcome ────────────────────────────────────────────────────────
    is_active: bool                         # True = published, False = discarded
    not_fully_novel: bool = False           # True = amber (rewritten / mixed verdict)
    discard_stage: str | None = Field(default=None, max_length=64)
    # relevance_score | grounding | novelty_embedding |
    # novelty_embedding_relevance | novelty_search | novelty_search_relevance

    # ── text ────────────────────────────────────────────────────────────────
    text: str = Field(sa_type=Text)         # final published text
    original_text: str = Field(default="", sa_type=Text)
    theme: str = Field(default="", max_length=256)

    # ── relevance scoring ────────────────────────────────────────────────────
    relevance_score: int | None = None      # 1-5
    relevance_passed: bool | None = None
    relevance_reason: str | None = Field(default=None, sa_type=Text)

    # ── entity grounding ────────────────────────────────────────────────────
    grounding_decision: str | None = Field(default=None, max_length=16)  # valid | invalid
    grounding_reason: str | None = Field(default=None, sa_type=Text)

    # ── novelty embedding ────────────────────────────────────────────────────
    embedding_decision: str | None = Field(default=None, max_length=16)  # keep | discard | rewrite
    embedding_reason: str | None = Field(default=None, sa_type=Text)
    embedding_rewritten: bool = False       # True if a rewrite was produced

    # ── novelty search ───────────────────────────────────────────────────────
    search_verdict: str | None = Field(default=None, max_length=32)      # keep | discard | rewrite
    search_overall_verdict: str | None = Field(default=None, max_length=32)  # novel | mixed | …
    search_reason: str | None = Field(default=None, sa_type=Text)
    search_duration_seconds: float | None = None
    # Post-rewrite relevance check — only present when search_verdict == "rewrite".
    # This is the LAST relevance check the bullet passed; display it instead of
    # the initial relevance_score for rewritten bullets.
    search_relevance_score: int | None = None
    search_relevance_reason: str | None = Field(default=None, sa_type=Text)

    # ── display data (JSON) ──────────────────────────────────────────────────
    # These columns store the full nested data needed to render a bullet in the
    # UI without ever touching output_json again. Written once at flush time.

    # [{id, headline, text, source_name, date}] — citations resolved via source_references
    citations_json: str = Field(default="[]", sa_type=Text)

    # [{evaluator_name, decision, reason, retrieved_bullets:[{text,score,date}]}]
    evaluator_details_json: str = Field(default="[]", sa_type=Text)

    # [{claim_index, claim_text, novelty, reasoning, evidence_ids:[str]}]
    claim_verdicts_json: str = Field(default="[]", sa_type=Text)

    # {simple_id: {headline, date, text}} — lookup table for evidence_ids
    evidence_map_json: str = Field(default="{}", sa_type=Text)

    # [str] — citation IDs referenced during grounding check
    grounding_citations_json: str = Field(default="[]", sa_type=Text)

    created_at: datetime


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


class SQLRunMetrics(SQLModel, table=True):
    """Cost and usage metrics for one pipeline run.

    Written once at run completion via _flush_run_metrics(). One row per run.
    Enables cost tracking per entity, per model, and per time window without
    parsing the multi-MB output_json.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(index=True)   # FK → SQLEntityPipelineRunLog.run_id
    entity_id: str = Field(index=True, max_length=64)
    report_window_start: datetime
    report_window_end: datetime

    # LLM usage: JSON array of {model, prompt_tokens, completion_tokens, total_tokens, n_calls, cost_usd}
    llm_per_model_json: str = Field(default="[]", sa_type=Text)

    # Embedding usage
    embedding_model: str = Field(default="N/A", max_length=128)
    embedding_tokens: int = Field(default=0)
    embedding_cost_usd: float = Field(default=0.0)

    # Chunks retrieved across all search phases (exploratory + concept + novelty search)
    chunks_total: int = Field(default=0)

    # Per-step breakdown: JSON dict of {step_name: {llm_cost_usd, llm_tokens, chunks_retrieved, ...}}
    step_detail_json: str = Field(default="{}", sa_type=Text)

    # Scalar totals for easy filtering / aggregation
    total_llm_cost_usd: float = Field(default=0.0)
    total_embedding_cost_usd: float = Field(default=0.0)
    total_cost_usd: float = Field(default=0.0)

    created_at: datetime

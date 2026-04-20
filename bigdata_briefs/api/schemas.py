"""Request and response Pydantic models for the pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from bigdata_briefs.orchestration.windows import WindowMode


# ── Trigger run ───────────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    """Body for POST /entities/{entity_id}/run."""

    pipeline_config: dict[str, Any] | None = None  # None → load default from disk
    state_dir: str | None = None
    refresh_entity: bool = False
    force_run: bool = False
    force_window_start: datetime | None = None
    force_window_end: datetime | None = None
    window_mode: WindowMode = WindowMode.DAILY


class RunSubmittedResponse(BaseModel):
    run_id: str
    entity_id: str
    status: str = "accepted"


# ── Run status ────────────────────────────────────────────────────────────────


class RunStatusResponse(BaseModel):
    run_id: str
    entity_id: str
    status: str  # running | succeeded | failed
    window_start: datetime
    window_end: datetime
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None  # short error (first line only)
    error_traceback: str | None = None  # full traceback when available
    exit_code: int | None = None


# ── Entity history ────────────────────────────────────────────────────────────


class RunSummary(BaseModel):
    run_id: str
    status: str
    window_start: datetime
    window_end: datetime
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    exit_code: int | None = None


class EntityRunsResponse(BaseModel):
    entity_id: str
    total: int
    runs: list[RunSummary]


# ── Latest bullets ────────────────────────────────────────────────────────────


class CitationDetail(BaseModel):
    """A resolved citation with its source title and chunk text."""

    id: str       # e.g. "CQS:REF0"
    headline: str  # article / document title
    text: str      # chunk text used as evidence


class BulletPointItem(BaseModel):
    """A single bullet point from a completed run."""

    trace_id: str
    text: str
    citations: list[CitationDetail]
    embedding_decision: str | None  # keep | rewrite | discard
    search_action: str | None       # keep | rewrite | discard | None
    # True when novelty_search kept the bullet (search_action=="keep") but the
    # overall claim-level verdict was "mixed" — i.e. at least one claim was already
    # known in the evidence.  Fully novel bullets have this as False.
    not_fully_novel: bool = False


class LatestBulletsResponse(BaseModel):
    """Bullet points from the latest successful run for an entity."""

    entity_id: str
    entity_name: str
    run_id: str
    report_window_start: datetime
    report_window_end: datetime
    run_created_at: datetime
    bullet_count: int
    bullets: list[BulletPointItem]


# ── Delete entity ─────────────────────────────────────────────────────────────


class DeleteEntityResponse(BaseModel):
    """Rows deleted per table when purging an entity."""

    entity_id: str
    deleted: dict[str, int]  # table_name -> rows deleted
    total_deleted: int


# ── Universes ─────────────────────────────────────────────────────────────────


class UniverseResponse(BaseModel):
    """A named universe of entity IDs."""

    name: str
    entity_ids: list[str]
    total: int


class UniverseListResponse(BaseModel):
    """All registered universes."""

    universes: list[UniverseResponse]


# ── Batch run ────────────────────────────────────────────────────────────────


class BatchRunRequest(BaseModel):
    """Body for POST /batch/run."""

    entity_ids: list[str]
    pipeline_config: dict[str, Any] | None = None
    state_dir: str | None = None
    force_run: bool = False
    force_window_start: datetime | None = None  # override report window start (ISO 8601)
    force_window_end: datetime | None = None    # override report window end   (ISO 8601)
    window_mode: WindowMode = WindowMode.DAILY


class BatchRunResponse(BaseModel):
    """One submission entry per entity."""

    submitted: list[RunSubmittedResponse]
    total: int


# ── Batch status ─────────────────────────────────────────────────────────────


class BatchStatusRequest(BaseModel):
    """Body for POST /batch/status."""

    run_ids: list[str]


class BatchRunStatusItem(BaseModel):
    """Status of a single run within a batch."""

    run_id: str
    entity_id: str | None = None
    status: str              # running | succeeded | failed | not_found
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BatchStatusResponse(BaseModel):
    """Aggregated progress of a batch run."""

    total: int
    succeeded: int
    failed: int
    running: int
    not_found: int
    runs: list[BatchRunStatusItem]


# ── Batch bullets ─────────────────────────────────────────────────────────────


class RunBulletsResult(BaseModel):
    """Bullets from a single run."""

    run_id: str
    report_window_start: datetime
    report_window_end: datetime
    run_created_at: datetime
    bullet_count: int
    # Explicit saved / discarded counts (bullet_count == bullets_saved for convenience).
    bullets_saved: int = 0
    bullets_discarded: int = 0
    bullets: list["BulletPointItem"]
    # Discarded bullets grouped by the stage that eliminated them.
    # Each entry is just the bullet text — no explanations.
    discarded_by_relevance: list[str] = []
    discarded_by_grounding: list[str] = []
    discarded_by_novelty: list[str] = []


class EntityBulletsResult(BaseModel):
    """All runs and their bullets for a single entity (or not-found marker)."""

    entity_id: str
    found: bool
    entity_name: str | None = None
    total_runs: int = 0
    total_bullets: int = 0
    runs: list[RunBulletsResult] = []


class BatchBulletsRequest(BaseModel):
    """Body for POST /batch/bullets."""

    entity_ids: list[str]


class BatchBulletsResponse(BaseModel):
    results: list[EntityBulletsResult]
    total_entities: int
    total_bullets: int


# ── Run bullet trace ─────────────────────────────────────────────────────────


class RelevanceScoringTrace(BaseModel):
    score: int
    reason: str
    passed: bool


class GroundingTrace(BaseModel):
    decision: str                        # valid | invalid
    reason: str


class EmbeddingJudgmentTrace(BaseModel):
    decision: str                        # keep | discard | rewrite
    reason: str
    evaluator_details: list[dict] = []


class EmbeddingTrace(BaseModel):
    judgment: EmbeddingJudgmentTrace | None = None
    rewritten_text: str | None = None    # set when judgment was "rewrite"
    relevance_score: int | None = None   # set after rewrite, when relevance was checked
    relevance_passed: bool | None = None


class SearchTrace(BaseModel):
    verdict: str                         # keep | rewrite | discard
    rewritten_text: str | None = None
    duration_seconds: float | None = None
    reason: str | None = None            # explanation from the novelty-via-search subgraph
    details: dict | None = None          # full raw subgraph output (sanitized)
    relevance_score: int | None = None
    relevance_passed: bool | None = None


class BulletTrace(BaseModel):
    """Full step-by-step trace for a single bullet across all pipeline nodes."""

    trace_id: str
    is_active: bool
    theme: str
    text: str                            # final text (post all rewrites)
    citations: list[str]

    relevance_scoring: RelevanceScoringTrace | None = None
    grounding: GroundingTrace | None = None
    embedding: EmbeddingTrace | None = None
    search: SearchTrace | None = None
    failure: dict[str, Any] | None = None


class RunTraceResponse(BaseModel):
    """Full per-bullet trace for a pipeline run."""

    run_id: str
    entity_id: str
    total_bullets: int
    active_bullets: int
    bullets: list[BulletTrace]


# ── Admin ────────────────────────────────────────────────────────────────────


class ResetDatabaseResponse(BaseModel):
    """Result of a full database reset."""

    tables_dropped: list[str]
    tables_recreated: list[str]
    total_tables: int


class ClearStaleRunsResponse(BaseModel):
    """Result of POST /admin/clear-stale-runs."""

    cleared: int
    """Number of ``running`` rows that were reset to ``failed``."""
    entity_ids: list[str]
    """Entity IDs whose stale run rows were cleared."""
    stale_seconds_threshold: int
    """Age (seconds) above which a running row was considered stale."""


# ── Dry run ───────────────────────────────────────────────────────────────────


class DryRunRequest(BaseModel):
    """Body for POST /entities/{entity_id}/dry-run."""

    force_window_start: datetime | None = None
    force_window_end: datetime | None = None
    window_mode: WindowMode = WindowMode.DAILY


class DryRunResponse(BaseModel):
    entity_id: str
    window_start: datetime
    window_end: datetime
    previous_bullets: list[dict[str, Any]]


# ── Rate limiter observability ────────────────────────────────────────────────


class RateStatusResponse(BaseModel):
    """Snapshot of the process-global Bigdata rate-limit budget and worker pool.

    Use this to size ``MAX_CONCURRENT_ENTITIES`` empirically: run a parallel
    batch and watch whether ``queries_in_recent_window`` pegs at
    ``window_capacity`` (you're saturating the 450 QPM cap and should lower
    concurrency or accept queuing).
    """

    # ── Bigdata 450 QPM window ──
    queries_in_recent_window: int
    window_capacity: int
    window_seconds: float

    # ── Connection pool ──
    connection_sem_capacity: int
    connection_sem_available: int | None  # None if the platform doesn't expose it

    # ── Entity worker pool ──
    max_concurrent_entities: int
    entities_in_flight: int  # futures submitted and not yet done
    entity_queue_depth: int  # futures waiting for a worker slot

"""
Node IDs, phase names, and status literals for the Brief 2.0 LangGraph pipeline.
"""

from typing import Literal


# ── Pipeline Status ──────────────────────────────────────────────────────────

PipelineStatus = Literal["running", "completed", "no_data", "error"]

PIPELINE_STATUS_RUNNING = "running"
PIPELINE_STATUS_COMPLETED = "completed"
PIPELINE_STATUS_NO_DATA = "no_data"
PIPELINE_STATUS_ERROR = "error"


# ── Conditional Edge Routes ───────────────────────────────────────────────────

ROUTE_CONTINUE = "continue"
ROUTE_NO_DATA = "no_data"
ROUTE_BUILD_REPORT = "build_report"
ROUTE_POST_PROCESSING = "post_processing"
ROUTE_NEXT_THEME = "next_theme"
ROUTE_DONE = "done"


# ── Node IDs ─────────────────────────────────────────────────────────────────

# Initialization
NODE_INITIALIZE_PIPELINE = "initialize_pipeline"

# Phase 1 — Search
NODE_INITIAL_CHECK = "initial_check"
NODE_EXPLORATORY_SEARCH = "exploratory_search"
NODE_QUARTER_INFO = "quarter_info"
NODE_CONCEPT_EXTRACTION = "concept_extraction"
NODE_CONCEPT_SEARCH = "concept_search"
NODE_CONCEPT_SEARCH_POSTPROCESSING = "concept_search_postprocessing"

# Phase 2 — Bullet Generation (subgraph)
NODE_BULLETS_GENERATION = "bullets_generation"
NODE_RELEVANCE_SCORE = "relevance_score"
SUBGRAPH_BULLET_GENERATION = "bullets_generation_and_scoring"

# Entity Grounding
NODE_ENTITY_GROUNDING_CHECK = "entity_grounding_check"

# Phase 3 — Novelty Embedding
NODE_EMBED_AND_RETRIEVE = "embed_and_retrieve"
NODE_NOVELTY_JUDGMENT_EMBEDDING = "novelty_judgment_embedding"
NODE_REWRITE_EMBEDDING = "rewrite_embedding"
NODE_RELEVANCE_CHECK_EMBEDDING = "relevance_check_embedding"
NODE_PERSIST_NOVEL_EMBEDDINGS = "persist_novel_embeddings"

# Phase 4 — Novelty via Search
NODE_NOVELTY_VIA_SEARCH = "novelty_via_search"  # kept for retrocompatibilità
NODE_NOVELTY_SEARCH_PARSE_AND_PLAN = "novelty_search_parse_and_plan"
NODE_NOVELTY_SEARCH_FETCH = "novelty_search_fetch"
NODE_NOVELTY_SEARCH_JUDGMENT = "novelty_search_judgment"
NODE_NOVELTY_SEARCH_REWRITE = "novelty_search_rewrite"
NODE_RELEVANCE_SCORE_SEARCH = "relevance_score_search"

# Finalization
NODE_SAVE_NOVEL_BULLETS = "save_novel_bullets"

# Post-Processing (optional)
NODE_REDUNDANCY_CHECK = "redundancy_check"
NODE_THEMATIC_CONSOLIDATION = "thematic_consolidation"
NODE_STANDALONE_VALIDATION = "standalone_validation"

# Output
NODE_BUILD_REPORT = "build_report"


# ── Service Types (for NodeMetricsRecord) ────────────────────────────────────

ServiceType = Literal["llm", "search", "embed", "none"]

SERVICE_TYPE_LLM = "llm"
SERVICE_TYPE_SEARCH = "search"
SERVICE_TYPE_EMBED = "embed"
SERVICE_TYPE_NONE = "none"


# ── Bullet Decisions ──────────────────────────────────────────────────────────

DECISION_KEEP = "keep"
DECISION_DISCARD = "discard"
DECISION_REWRITE = "rewrite"
DECISION_VALID = "valid"
DECISION_INVALID = "invalid"

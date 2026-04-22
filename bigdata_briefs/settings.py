from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TOPICS = ["{entity}"]

PROJECT_DIRECTORY = Path(__file__).resolve().parent.parent

# Default SQLite file next to ``pyproject.toml``, not relative to shell cwd (``sqlite:///briefs.db`` would be).
_DEFAULT_DB_SQLITE_URL = f"sqlite:///{(PROJECT_DIRECTORY / 'briefs.db').resolve().as_posix()}"

UNSET: Literal["<UNSET>"] = "<UNSET>"


class Settings(BaseSettings):
    """Loads optional ``.env`` from the package project root (next to ``pyproject.toml``)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIRECTORY / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BIGDATA_API_KEY: str | Literal["<UNSET>"] = UNSET
    OPENAI_API_KEY: str | Literal["<UNSET>"] = UNSET
    # FastAPI layer: set to a non-empty string to require X-API-Key header on all routes.
    # Leave empty to disable authentication (e.g. internal/local use).
    PIPELINE_API_KEY: str = ""

    # Data storage configuration (override with ``DB_STRING`` in ``.env`` if needed)
    DB_STRING: str = _DEFAULT_DB_SQLITE_URL
    # Pipeline JSON state directory for ``PipelineRunner`` when using entity orchestrator CLI.
    BRIEF_PIPELINE_STATE_DIR: str = ""
    # Seconds after which a ``running`` row in ``SQLEntityPipelineRunLog`` is treated as stale.
    # Default: 3600 s (1 h) — a run that hasn't finished in an hour is almost certainly
    # from a dead/restarted server process and should be cleared automatically.
    ORCHESTRATION_STALE_RUNNING_SECONDS: int = 3_600
    TEMPLATES_DIR: str = str(PROJECT_DIRECTORY / "bigdata_briefs" / "templates")

    # General configuration
    TOPICS: list[str] = DEFAULT_TOPICS
    INTRO_SECTION_MIN_RELEVANCE_SCORE: int = 3
    # After novelty_check: redundancy, theme consolidation, standalone validation (modular steps 8–10
    # and matching logic in BriefPipelineService.process_bullet_points). Default off: main workflow
    # ends when bullet generation + novelty completes.
    ENABLE_BULLET_PROCESSING_PHASE: bool = False

    # Novelty configuration (LLM novelty always runs when the pipeline reaches novelty check)
    MAX_NOVELTY_WORKERS: int = 10  # Max parallel LLM calls for novelty check
    NOVELTY_MODEL: str = "text-embedding-3-large"
    NOVELTY_LOOKBACK_DAYS: int = 14

    # Novelty-via-search (LangGraph) after brief LLM novelty; ``novelty-via-search`` dist / ``novelty_via_search`` package.
    # Must remain true (validated); do not set ``NOVELTY_SEARCH_ENABLED=false`` in env.
    NOVELTY_SEARCH_ENABLED: bool = True
    # Cap concurrent novelty-via-search LangGraph invocations per entity.
    # Each invocation makes multiple LLM calls internally; without this cap
    # MAX_CONCURRENT_ENTITIES * bullets * llm_calls_per_bullet concurrent
    # OpenAI requests would saturate TPM and stall. Keep ≤ 10.
    # Bigdata QPM is enforced separately via the shared RequestsPerMinuteController.
    NOVELTY_SEARCH_MAX_CONCURRENT: int = 10
    # After novelty search (LangGraph): ``relevance_check`` on rewritten text (``novelty_search_rewrite_relevance_check_*``).
    # Only when pipeline verdict is ``mixed`` and the rewrite differs materially from the bullet sent in.
    NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_ENABLED: bool = True
    # Max parallel relevance LLM calls for that path (per entity batch).
    NOVELTY_SEARCH_REWRITE_RELEVANCE_CHECK_MAX_CONCURRENT: int = 5
    # When False (default): LangGraph state omits internal "relevance scoring" in novelty-via-search.
    # Set True only if you intentionally enable that graph path (not used by brief metrics / LLMClient).
    NOVELTY_LANGGRAPH_ENABLE_RELEVANCE_SCORING: bool = False
    EMBEDDING_RETRIES: int = 3
    
    # Novelty LLM pre-filtering (reduces tokens by filtering previous bullets by similarity)
    NOVELTY_PREFILTER_THRESHOLD: float = 0.5  # Min similarity to include in LLM prompt
    NOVELTY_PREFILTER_TOP_K: int = 10  # Max previous bullets to include per new bullet

    # Save all bullets (keep / discard_by_novelty / discard_by_relevance / rewrite) with status + evaluator_details
    SAVE_ALL_BULLETS_DEBUG: bool = True

    # Search configuration
    API_SIMULTANEOUS_REQUESTS: int = 40  # Reduced to prevent rate limit bursts
    API_BASE_URL: str = "https://api.bigdata.com"

    # Parallel entity execution (used by /api/v1/batch/run-parallel).
    # The Bigdata 450 QPM hard limit is NOT enforced by this value — it is enforced
    # by the process-global RequestsPerMinuteController wired in at FastAPI startup
    # (see api/app.py lifespan). This caps how many entity pipelines run concurrently.
    # Start conservative (OpenAI TPM is usually the real ceiling) and tune via
    # GET /api/v1/rate/status.
    MAX_CONCURRENT_ENTITIES: int = 4
    API_CHUNKS_LIMIT_EXPLORATORY: int = 15
    API_RERANK_EXPLORATORY: float = 0.8
    EXPLORATORY_SENTIMENT_THRESHOLD: float = 0.0
    API_CHUNK_LIMIT_FOLLOWUP: int = 15
    FOLLOWUP_SENTIMENT_THRESHOLD: float = 0.0
    API_SOURCE_RANK_BOOST: int = 10
    API_FRESHNESS_BOOST: int = 8

    # Before bullet LLM prompts: prefer documents whose ``source_rank`` is in [min, max]
    # (inclusive). Unknown rank (None) is dropped only when strict filtering applies **for that theme**.
    # Per theme: if fewer than ``MIN_CHUNKS`` chunks fall in the rank window, that theme keeps all ranks.
    BULLET_GENERATION_FILTER_SOURCE_RANK: bool = True
    BULLET_GENERATION_SOURCE_RANK_MIN: int = 1
    BULLET_GENERATION_SOURCE_RANK_MAX: int = 2
    BULLET_GENERATION_SOURCE_RANK_MIN_CHUNKS: int = 3
    API_RETRIES: int = 3
    API_TIMEOUT_SECONDS: int = 120

    # LLM configuration
    LLM_RETRIES: int = 4  # attempt 0→fail→30s, attempt 1→fail→60s, attempt 2→fail→60s, attempt 3→raise
    LLM_TIMEOUT_SECONDS: int = 60
    EMBEDDING_TIMEOUT_SECONDS: int = 60
    NOVELTY_SEARCH_TIMEOUT_SECONDS: int = 120
    NOVELTY_SEARCH_HTTP_TIMEOUT_SECONDS: int = 30
    RUN_RANGE_DAY_TIMEOUT_SECONDS: int = 1800  # hard wall-clock limit per day (30 min)

    # When workflow passes rerank_concept_sources=True (see QueryService.run_concept_queries_*).
    RERANK_CONCEPT_CHUNK_LIMIT: int = 45  # Chunks to fetch per concept when reranking
    RERANK_CONCEPT_THRESHOLD: float = 0.7  # Lower threshold to get more candidates
    RERANK_CONCEPT_LIMIT_PER_CONCEPT: int = 15  # Final limit per concept after reranking
    
    # Same-text deduplication configuration
    # When True, chunks with identical text but different sources are merged into a single prompt entry
    # with multiple headlines, reducing redundancy and improving LLM focus
    DEDUPLICATE_SAME_TEXT: bool = True

    # Chunk hash storage configuration
    # When True, stores SHA256 hashes of chunk texts to filter out already-used chunks in subsequent runs
    STORE_RETRIEVED_CHUNKS: bool = True
    CHUNK_HASH_LOOKBACK_DAYS: int = 14  # Same lookback as novelty check

    # Named entity ID lists for the web UI dropdown.
    # Set as JSON string in environment: ENTITY_LISTS='{"sp500": ["D8442A", ...], "tech": [...]}'
    ENTITY_LISTS: dict[str, list[str]] = {}

    @classmethod
    def load_from_env(cls) -> "Settings":
        return cls()

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        if self.BIGDATA_API_KEY == UNSET or self.OPENAI_API_KEY == UNSET:
            raise ValueError("BIGDATA_API_KEY and OPENAI_API_KEY must be set.")
        if not self.NOVELTY_SEARCH_ENABLED:
            raise ValueError(
                "NOVELTY_SEARCH_ENABLED must be true: novelty-via-search cannot be disabled."
            )
        return self


settings = Settings.load_from_env()

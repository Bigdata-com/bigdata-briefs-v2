"""Constants and context manager for novelty_check wall-clock substeps (aggregated, not per-call)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from bigdata_briefs.metrics import EntityStepMetrics

# Substep names persisted under pipeline_step == "novelty_check"
NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION = "novelty_embedding_evaluation"
NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE = "novelty_embedding_rewrite"
NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE_RELEVANCE_CHECK = (
    "novelty_embedding_rewrite_relevance_check"
)
NOVELTY_WALL_SUBSTEP_SEARCH_LANGGRAPH = "novelty_search_langgraph"
NOVELTY_WALL_SUBSTEP_SEARCH_REWRITE_RELEVANCE_CHECK = (
    "novelty_search_rewrite_relevance_check"
)


@contextmanager
def track_novelty_wall_substep(
    entity_metrics: "EntityStepMetrics | None",
    substep: str,
) -> Generator[None, None, None]:
    """Wall-clock span for one novelty substep; accumulates into ``entity_metrics``."""
    if entity_metrics is None:
        yield
        return
    started = datetime.now(timezone.utc)
    try:
        yield
    finally:
        ended = datetime.now(timezone.utc)
        entity_metrics.accumulate_novelty_substep_wall(substep, started, ended)

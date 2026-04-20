"""Canonical ``step_name`` strings for novelty-related LLM calls and LangGraph timing."""

from __future__ import annotations

# Three-window evaluator internal names → embedding novelty Step 1 suffix
_EMBEDDING_EVALUATOR_SUFFIX: dict[str, str] = {
    "llm_novelty_window": "novelty_window",
    "llm_remaining_window": "remaining_window",
    "llm_full_history": "full_history",
}


def novelty_embedding_evaluation_step_name(
    evaluator_name: str,
    bullet_index: int | None,
) -> str:
    """Step name for embedding-based novelty Step 1 (per evaluator)."""
    suffix = _EMBEDDING_EVALUATOR_SUFFIX.get(evaluator_name)
    if suffix is None:
        tail = evaluator_name[4:] if evaluator_name.startswith("llm_") else evaluator_name
        base = f"novelty_embedding_evaluation_{tail}"
    else:
        base = f"novelty_embedding_evaluation_{suffix}"
    if bullet_index is not None:
        return f"{base}_{bullet_index}"
    return base


def novelty_embedding_rewrite_step_name(bullet_index: int | None) -> str:
    """Step name for embedding novelty Step 2 (rewrite)."""
    if bullet_index is not None:
        return f"novelty_embedding_rewrite_{bullet_index}"
    return "novelty_embedding_rewrite"


def novelty_embedding_rewrite_relevance_check_step_name(bullet_index: int | None) -> str:
    """Step name for post-rewrite relevance LLM after embedding novelty rewrite."""
    if bullet_index is not None:
        return f"novelty_embedding_rewrite_relevance_check_{bullet_index}"
    return "novelty_embedding_rewrite_relevance_check"


def novelty_search_evaluation_and_rewrite_step_name(bullet_index: int) -> str:
    """Per-bullet timing label for LangGraph ``ainvoke`` (brief-side metrics, not graph nodes)."""
    return f"novelty_search_evaluation_and_rewrite_{bullet_index}"


def novelty_search_rewrite_relevance_check_step_name(bullet_index: int) -> str:
    """Step name for mixed-verdict post-search relevance check on rewritten text."""
    return f"novelty_search_rewrite_relevance_check_{bullet_index}"

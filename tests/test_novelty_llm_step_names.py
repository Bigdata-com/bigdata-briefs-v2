"""Unit tests for canonical novelty-related LLM / LangGraph step name helpers."""

from __future__ import annotations

from bigdata_briefs.novelty.step_names import (
    novelty_embedding_evaluation_step_name,
    novelty_embedding_rewrite_relevance_check_step_name,
    novelty_embedding_rewrite_step_name,
    novelty_search_evaluation_and_rewrite_step_name,
    novelty_search_rewrite_relevance_check_step_name,
)


def test_novelty_embedding_evaluation_three_window() -> None:
    assert novelty_embedding_evaluation_step_name("llm_novelty_window", 2) == (
        "novelty_embedding_evaluation_novelty_window_2"
    )
    assert novelty_embedding_evaluation_step_name("llm_remaining_window", None) == (
        "novelty_embedding_evaluation_remaining_window"
    )
    assert novelty_embedding_evaluation_step_name("llm_full_history", 0) == (
        "novelty_embedding_evaluation_full_history_0"
    )


def test_novelty_embedding_unknown_evaluator_strips_llm_prefix() -> None:
    assert novelty_embedding_evaluation_step_name("llm_custom", 1) == (
        "novelty_embedding_evaluation_custom_1"
    )


def test_novelty_embedding_rewrite_and_relevance() -> None:
    assert novelty_embedding_rewrite_step_name(5) == "novelty_embedding_rewrite_5"
    assert novelty_embedding_rewrite_step_name(None) == "novelty_embedding_rewrite"
    assert novelty_embedding_rewrite_relevance_check_step_name(3) == (
        "novelty_embedding_rewrite_relevance_check_3"
    )
    assert novelty_embedding_rewrite_relevance_check_step_name(None) == (
        "novelty_embedding_rewrite_relevance_check"
    )


def test_novelty_search_labels() -> None:
    assert novelty_search_evaluation_and_rewrite_step_name(7) == (
        "novelty_search_evaluation_and_rewrite_7"
    )
    assert novelty_search_rewrite_relevance_check_step_name(4) == (
        "novelty_search_rewrite_relevance_check_4"
    )

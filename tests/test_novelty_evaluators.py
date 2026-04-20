"""Tests for novelty evaluators and aggregate_evaluator_results (veto + REWRITE pick-one)."""

import pytest

from bigdata_briefs.novelty.evaluators import (
    NoveltyContext,
    NoveltyEvaluatorResult,
    aggregate_evaluator_results,
)


def _res(
    decision: str,
    reason: str = "",
    rewritten_text: str | None = None,
    evaluator_name: str = "test",
) -> NoveltyEvaluatorResult:
    return NoveltyEvaluatorResult(
        decision=decision,
        reason=reason,
        rewritten_text=rewritten_text,
        evaluator_name=evaluator_name,
    )


class TestAggregateEvaluatorResults:
    """Unit tests for aggregate_evaluator_results."""

    def test_empty_results_keeps_original(self):
        text, decision, reason = aggregate_evaluator_results([], "original bullet")
        assert text == "original bullet"
        assert decision == "KEEP"
        assert "No evaluator" in reason

    def test_all_keep_returns_original(self):
        results = [
            _res("KEEP", "new", evaluator_name="a"),
            _res("KEEP", "novel", evaluator_name="b"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "bullet")
        assert text == "bullet"
        assert decision == "KEEP"
        assert "a" in reason and "b" in reason

    def test_one_discard_veto_discards(self):
        results = [
            _res("KEEP", "ok", evaluator_name="a"),
            _res("DISCARD", "already seen", evaluator_name="b"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "bullet")
        assert text == ""
        assert decision == "DISCARD"
        assert "Veto" in reason and "b" in reason

    def test_multiple_discard_veto(self):
        results = [
            _res("DISCARD", "dup1", evaluator_name="e1"),
            _res("DISCARD", "dup2", evaluator_name="e2"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "bullet")
        assert text == ""
        assert decision == "DISCARD"
        assert "e1" in reason and "e2" in reason

    def test_one_rewrite_returns_rewritten(self):
        results = [
            _res("KEEP", "ok", evaluator_name="a"),
            _res("REWRITE", "partial", rewritten_text="New text only.", evaluator_name="b"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "original")
        assert text == "New text only."
        assert decision == "REWRITE"
        assert "b" in reason

    def test_multiple_rewrite_picks_shortest_inline(self):
        results = [
            _res("REWRITE", "r1", rewritten_text="Part one.", evaluator_name="e1"),
            _res("REWRITE", "r2", rewritten_text="Part two longer.", evaluator_name="e2"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "original")
        assert text == "Part one."
        assert decision == "REWRITE"
        assert "e1" in reason and "e2" in reason

    def test_rewrite_join_parameter_reserved_unused(self):
        results = [
            _res("REWRITE", "r1", rewritten_text="A", evaluator_name="e1"),
            _res("REWRITE", "r2", rewritten_text="B", evaluator_name="e2"),
        ]
        text, _, _ = aggregate_evaluator_results(
            results, "x", rewrite_join=" | "
        )
        # rewrite_join is reserved; policy is pick-one (stable tie-break: first shortest).
        assert text == "A"

    def test_discard_beats_rewrite_veto(self):
        results = [
            _res("REWRITE", "partial", rewritten_text="Rewritten.", evaluator_name="a"),
            _res("DISCARD", "duplicate", evaluator_name="b"),
        ]
        text, decision, reason = aggregate_evaluator_results(results, "original")
        assert text == ""
        assert decision == "DISCARD"
        assert "Veto" in reason

    def test_rewrite_empty_string_fallback_to_original(self):
        results = [
            _res("REWRITE", "r1", rewritten_text="", evaluator_name="e1"),
            _res("REWRITE", "r2", rewritten_text="", evaluator_name="e2"),
        ]
        text, decision, _ = aggregate_evaluator_results(results, "original")
        assert text == "original"
        assert decision == "REWRITE"


class TestNoveltyContext:
    """Smoke test for NoveltyContext dataclass."""

    def test_context_creation(self):
        from datetime import datetime

        ctx = NoveltyContext(
            entity_id="e1",
            entity_name="Entity One",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 31),
            current_date=datetime(2024, 1, 31),
            lookback_days=14,
            clean_up_func=None,
        )
        assert ctx.entity_id == "e1"
        assert ctx.bullet_index is None
        assert ctx.precomputed_embedding is None

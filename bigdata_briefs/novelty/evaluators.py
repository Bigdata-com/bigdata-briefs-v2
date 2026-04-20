"""
Pluggable novelty evaluation: Retrievers (fetch relevant previous bullets) + LLM Judge (same LLM for all).
Each evaluator = one Retriever + one Judge; multiple retrievers (e.g. embedding 14d, embedding 7d, tf-idf)
feed the same LLM in parallel.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal, Protocol, TYPE_CHECKING

import numpy as np

from bigdata_briefs import logger
from bigdata_briefs.novelty.step_names import (
    novelty_embedding_evaluation_step_name,
    novelty_embedding_rewrite_step_name,
)


def _debug_log_slug(entity_name: str) -> str:
    """Same logic as DebugLogger folder and run script company_slug: safe path segment."""
    sanitized = re.sub(r"[^\w\-]", "_", entity_name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "entity"


from bigdata_briefs.models import NoveltyClassification, NoveltyRewrite

# Counters for novelty parse reliability (exposed for monitoring; reset only on process restart)
_novelty_parse_attempts = 0
_novelty_parse_failures = 0
# Counters for Step 2 (two-step mode) parse reliability
_novelty_step2_parse_attempts = 0
_novelty_step2_parse_failures = 0
# Count of REWRITE decisions where rewritten_text was identical to original (model quality signal)
_novelty_rewrite_identity_count = 0
# Count of REWRITE decisions overridden to DISCARD because Step 2 returned is_empty=True
_novelty_step2_empty_count = 0
# Count of Step 1 calls that returned REWRITE/DISCARD with empty evidence_ids (triggers retry when > 0)
_novelty_step1_missing_evidence_count = 0
# Retries for Step 1 when response has REWRITE/DISCARD but empty evidence_ids. Set to 0 to accept and track only.
STEP1_MISSING_EVIDENCE_RETRIES = 0


def get_novelty_step1_missing_evidence_count() -> int:
    """Return the number of Step 1 calls that returned REWRITE/DISCARD with empty evidence_ids (for monitoring)."""
    return _novelty_step1_missing_evidence_count


def _strip_double_asterisks(text: str | None) -> str | None:
    """Remove markdown bold (**) from LLM output. Returns None if input is None or empty after strip."""
    if not text:
        return None
    cleaned = text.replace("**", "").strip()
    return cleaned if cleaned else None


if TYPE_CHECKING:
    from bigdata_briefs.debug_logger import DebugLogger
    from bigdata_briefs.llm_client import LLMClient
    from bigdata_briefs.metrics import EntityStepMetrics
    from bigdata_briefs.novelty.embedding_client import EmbeddingClient
    from bigdata_briefs.novelty.models import BulletPointEmbedding
    from bigdata_briefs.novelty.storage import EmbeddingStorage


# --- Result and context (shared by retrievers and judge) ---

# Lower bound for "full history" / "remaining" queries (storage always needs start_date/end_date)
MIN_STORAGE_DATE = datetime(2000, 1, 1)


@dataclass
class NoveltyContext:
    """Context for retrieval and judgment (one bullet)."""

    entity_id: str
    entity_name: str
    start_date: datetime
    end_date: datetime
    current_date: datetime
    lookback_days: int
    clean_up_func: Callable[[str], str] | None
    debug_logger: "DebugLogger | None" = None
    entity_metrics: "EntityStepMetrics | None" = None
    bullet_index: int | None = None
    # Optional: service can precompute so all retrievers sharing embeddings avoid recompute
    precomputed_embedding: list[float] | None = None
    # Contextual quarter for "today" (e.g. "Q1 2026") for the novelty prompt
    current_quarter_title: str | None = None


@dataclass
class NoveltyEvaluatorResult:
    """Result from one evaluator (retriever+judge) for one bullet."""

    decision: Literal["KEEP", "DISCARD", "REWRITE"]
    reason: str
    rewritten_text: str | None
    evaluator_name: str
    # Bullets retrieved by this evaluator's retriever (before LLM judgment); None if not tracked
    retrieved_bullets: list[RetrievedBullet] | None = None
    # IDs of previous bullets that support this decision (for DISCARD/REWRITE); from LLM evidence_ids
    evidence_ids: list[str] | None = None
    # For REWRITE: removal instructions from Step 1 (saved in evaluator_details for UI)
    instruction: str | None = None


def aggregate_evaluator_results(
    results: list[NoveltyEvaluatorResult],
    original_text: str,
    *,
    rewrite_join: str = " ",
) -> tuple[str, Literal["KEEP", "DISCARD", "REWRITE"], str]:
    """
    Veto: any DISCARD -> discard. Else any REWRITE -> pick one rewritten text (full_history
    preferred if non-empty; else shortest non-empty inline rewrite). If all REWRITE votes
    have no inline text (two-step Step 1), returns (original_text, REWRITE, ...).
    Else KEEP.

    ``rewrite_join`` is reserved for a future multi-rewrite merge; currently unused.
    """
    _ = rewrite_join
    global _novelty_rewrite_identity_count
    if not results:
        return original_text, "KEEP", "No evaluator results"

    discard_votes = [r for r in results if r.decision == "DISCARD"]
    if discard_votes:
        veto_reasons = "; ".join(
            f"{r.evaluator_name}: {r.reason}" for r in discard_votes
        )
        return "", "DISCARD", f"Veto (discard): {veto_reasons}"

    rewrite_votes = [r for r in results if r.decision == "REWRITE"]
    if rewrite_votes:
        # When multiple REWRITE: prefer full_history if present, else shortest among time/remaining
        full_history_rewrite = next(
            (r for r in rewrite_votes if r.evaluator_name == "llm_full_history"),
            None,
        )
        if full_history_rewrite and full_history_rewrite.rewritten_text:
            chosen_text = (
                _strip_double_asterisks(full_history_rewrite.rewritten_text) or ""
            ).strip()
            if chosen_text:
                reasons = "; ".join(
                    f"{r.evaluator_name}: {r.reason}" for r in rewrite_votes
                )
                if chosen_text.strip() == original_text.strip():
                    _novelty_rewrite_identity_count += 1
                    logger.warning(
                        "[novelty] REWRITE identity: rewritten text identical to original (count=%s)",
                        _novelty_rewrite_identity_count,
                    )
                return chosen_text, "REWRITE", reasons
        # No full_history rewrite or empty: pick shortest among (novelty_window, remaining_window)
        candidates = [
            r
            for r in rewrite_votes
            if r.rewritten_text
            and (_strip_double_asterisks(r.rewritten_text) or "").strip()
        ]
        if not candidates:
            merged_text = original_text
        else:
            chosen = min(
                candidates,
                key=lambda r: len((_strip_double_asterisks(r.rewritten_text) or "").strip()),
            )
            merged_text = (
                _strip_double_asterisks(chosen.rewritten_text) or ""
            ).strip() or original_text
        reasons = "; ".join(
            f"{r.evaluator_name}: {r.reason}" for r in rewrite_votes
        )
        # Only warn when an evaluator supplied inline rewrite text equal to original (legacy/tests).
        # Two-step Step 1 has no inline text → merged_text == original is expected.
        if merged_text.strip() == original_text.strip() and candidates:
            _novelty_rewrite_identity_count += 1
            logger.warning(
                "[novelty] REWRITE identity: rewritten text identical to original (count=%s)",
                _novelty_rewrite_identity_count,
            )
        return merged_text, "REWRITE", reasons

    reasons = "; ".join(f"{r.evaluator_name}: {r.reason}" for r in results)
    return original_text, "KEEP", reasons


# --- Retriever: returns relevant previous bullets for one new bullet ---

# (text, score, date, earnings_call_date); earnings_call_date can be None for legacy
RetrievedBullet = tuple[str, float, datetime, str | None]


def _assign_bullet_ids(
    previous: list[RetrievedBullet],
) -> list[tuple[str, str, float, datetime, str | None]]:
    """
    Sort previous bullets by (date, text) and assign IDs A1, B2, C3, ...
    Returns list of (id, text, score, date, earnings_call_date) with deterministic ordering.
    """
    if not previous:
        return []
    sorted_list = sorted(previous, key=lambda x: (x[2], x[0]))  # (date, text)
    result: list[tuple[str, str, float, datetime, str | None]] = []
    for i, item in enumerate(sorted_list):
        text, score, dt = item[0], item[1], item[2]
        earnings = item[3] if len(item) > 3 else None
        letter = chr(ord("A") + i % 26)
        num = i + 1
        bid = f"{letter}{num}"
        result.append((bid, text, score, dt, earnings))
    return result


class NoveltyRetriever(Protocol):
    """Fetches relevant previous bullet points for a given new bullet (e.g. by embedding, tf-idf, window)."""

    @property
    def name(self) -> str:
        """Identifier for this retriever (used as evaluator name)."""
        ...

    def retrieve(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> list[RetrievedBullet]:
        """Return list of (text, score, date) for previous bullets to compare against."""
        ...


def _normalize_text_for_embedding(text: str, entity_name: str) -> str:
    """Prefix entity name for better similarity (same logic as novelty_service)."""
    text_lower = text.lower().strip()
    entity_lower = entity_name.lower()
    if text_lower.startswith(entity_lower) or text_lower.startswith(f"**{entity_lower}"):
        return text
    return f"{entity_name}: {text}"


def _prefilter_by_similarity(
    new_embedding: list[float],
    prev_bp_embeddings: list["BulletPointEmbedding"],
    threshold: float,
    top_k: int,
) -> list[RetrievedBullet]:
    """Top-K previous bullets with similarity >= threshold (most similar first)."""
    if not prev_bp_embeddings:
        return []
    new_emb = np.array(new_embedding)
    candidates: list[RetrievedBullet] = []
    for bp in prev_bp_embeddings:
        bp_emb = np.array(bp.embedding)
        sim = float(
            np.dot(new_emb, bp_emb)
            / (np.linalg.norm(new_emb) * np.linalg.norm(bp_emb) + 1e-12)
        )
        if sim >= threshold:
            earnings = getattr(bp, "earnings_call_date", None)
            candidates.append((bp.original_text, sim, bp.date, earnings))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:top_k]


class EmbeddingRetriever:
    """Retrieves relevant previous bullets via embedding similarity in a time window."""

    def __init__(
        self,
        embedding_client: "EmbeddingClient",
        storage: "EmbeddingStorage",
        *,
        threshold: float = 0.5,
        top_k: int = 10,
        name: str = "embedding",
    ):
        self.embedding_client = embedding_client
        self.storage = storage
        self.threshold = threshold
        self.top_k = top_k
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def retrieve(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> list[RetrievedBullet]:
        if context.precomputed_embedding is not None:
            new_embedding = context.precomputed_embedding
        else:
            text = (
                context.clean_up_func(bullet_text)
                if context.clean_up_func
                else bullet_text
            )
            normalized = _normalize_text_for_embedding(text, context.entity_name)
            new_embedding = self.embedding_client.compute(
                [normalized], entity_metrics=context.entity_metrics
            )[0]
        prev_bp = self.storage.retrieve(
            context.entity_id,
            start_date=context.start_date,
            end_date=context.end_date,
        )
        return _prefilter_by_similarity(
            new_embedding, prev_bp, self.threshold, self.top_k
        )


class EmbeddingRetrieverRemainingWindow:
    """Retrieves previous bullets in the window before the novelty window (remaining history)."""

    def __init__(
        self,
        embedding_client: "EmbeddingClient",
        storage: "EmbeddingStorage",
        *,
        threshold: float = 0.5,
        top_k: int = 10,
        name: str = "embedding_remaining",
    ):
        self.embedding_client = embedding_client
        self.storage = storage
        self.threshold = threshold
        self.top_k = top_k
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def retrieve(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> list[RetrievedBullet]:
        if context.precomputed_embedding is not None:
            new_embedding = context.precomputed_embedding
        else:
            text = (
                context.clean_up_func(bullet_text)
                if context.clean_up_func
                else bullet_text
            )
            normalized = _normalize_text_for_embedding(text, context.entity_name)
            new_embedding = self.embedding_client.compute(
                [normalized], entity_metrics=context.entity_metrics
            )[0]
        end_before_novelty = context.start_date - timedelta(microseconds=1)
        prev_bp = self.storage.retrieve(
            context.entity_id,
            start_date=MIN_STORAGE_DATE,
            end_date=end_before_novelty,
        )
        return _prefilter_by_similarity(
            new_embedding, prev_bp, self.threshold, self.top_k
        )


class EmbeddingRetrieverNoWindow:
    """Retrieves all previous bullets for the entity (no time window)."""

    def __init__(
        self,
        embedding_client: "EmbeddingClient",
        storage: "EmbeddingStorage",
        *,
        threshold: float = 0.5,
        top_k: int = 10,
        name: str = "embedding_full_history",
    ):
        self.embedding_client = embedding_client
        self.storage = storage
        self.threshold = threshold
        self.top_k = top_k
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def retrieve(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> list[RetrievedBullet]:
        if context.precomputed_embedding is not None:
            new_embedding = context.precomputed_embedding
        else:
            text = (
                context.clean_up_func(bullet_text)
                if context.clean_up_func
                else bullet_text
            )
            normalized = _normalize_text_for_embedding(text, context.entity_name)
            new_embedding = self.embedding_client.compute(
                [normalized], entity_metrics=context.entity_metrics
            )[0]
        prev_bp = self.storage.retrieve(
            context.entity_id,
            start_date=MIN_STORAGE_DATE,
            end_date=context.current_date,
        )
        return _prefilter_by_similarity(
            new_embedding, prev_bp, self.threshold, self.top_k
        )


# --- Judge: same LLM for all evaluators; only the retrieved set varies ---


class LLMNoveltyJudge:
    """LLM novelty: Step 1 classifies only (KEEP/DISCARD/REWRITE); Step 2 rewrites after aggregation.

    Judgment and rewrite use separate prompts: ``novelty_embedding_evaluation_prompt`` and
    ``novelty_embedding_rewrite_prompt``.
    """

    def __init__(self, llm_client: "LLMClient") -> None:
        self.llm_client = llm_client
        from bigdata_briefs.prompts.prompt_loader import get_prompt_keys

        self._step1_keys = get_prompt_keys("novelty_embedding_evaluation_prompt")
        self._step2_keys = get_prompt_keys("novelty_embedding_rewrite_prompt")

    def judge(
        self,
        bullet_text: str,
        previous_bullets_with_scores: list[RetrievedBullet],
        context: NoveltyContext,
        evaluator_name: str,
    ) -> NoveltyEvaluatorResult:
        """Run Step 1 LLM novelty classification; returns KEEP/DISCARD/REWRITE (no inline rewrite text)."""
        return self._judge_step1(
            bullet_text, previous_bullets_with_scores, context, evaluator_name
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt_context(
        bullet_text: str,
        previous: list[RetrievedBullet],
        context: NoveltyContext,
    ) -> tuple[str, list[dict[str, object]], str, str]:
        """Build shared prompt variables for Step 1.

        Returns (clean_text, previous_bulletpoints_by_date, current_datetime_str, current_quarter_title).
        """
        clean_text = (
            context.clean_up_func(bullet_text)
            if context.clean_up_func
            else bullet_text
        )
        with_ids = _assign_bullet_ids(previous)
        bullets_by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
        contextual_quarter_by_date: dict[str, str | None] = {}
        for row in with_ids:
            bid, bp_text, _score, bp_date = row[0], row[1], row[2], row[3]
            earnings = row[4] if len(row) > 4 else None
            date_str = bp_date.strftime("%Y-%m-%d")
            bullets_by_date[date_str].append({"id": bid, "text": bp_text})
            if date_str not in contextual_quarter_by_date:
                contextual_quarter_by_date[date_str] = earnings
        sorted_dates = sorted(bullets_by_date.keys())
        previous_bulletpoints_by_date: list[dict[str, object]] = [
            {
                "date": d,
                "contextual_quarter": contextual_quarter_by_date.get(d),
                "bullets": bullets_by_date[d],
            }
            for d in sorted_dates
        ]
        actual_current_date = context.current_date - timedelta(seconds=1)
        current_datetime_str = actual_current_date.strftime("%A, %B %d, %Y")
        current_quarter_title = getattr(context, "current_quarter_title", None) or "N/A"
        return clean_text, previous_bulletpoints_by_date, current_datetime_str, current_quarter_title

    def _judge_step1(
        self,
        bullet_text: str,
        previous_bullets_with_scores: list[RetrievedBullet],
        context: NoveltyContext,
        evaluator_name: str,
    ) -> NoveltyEvaluatorResult:
        """Two-step Step 1: classify only. rewritten_text is always None here."""
        previous = previous_bullets_with_scores
        if not previous:
            return NoveltyEvaluatorResult(
                decision="KEEP",
                reason="No similar previous bulletpoints found in lookback window",
                rewritten_text=None,
                evaluator_name=evaluator_name,
                evidence_ids=[],
            )

        clean_text, previous_bulletpoints_by_date, current_datetime_str, current_quarter_title = (
            self._build_prompt_context(bullet_text, previous, context)
        )

        user_prompt = self._step1_keys.user_template.render(
            current_datetime=current_datetime_str,
            current_quarter_title=current_quarter_title,
            entity_name=context.entity_name,
            previous_bulletpoints_by_date=previous_bulletpoints_by_date,
            new_bulletpoint=clean_text,
            lookback_days=context.lookback_days,
        )
        system = [{"role": "system", "content": self._step1_keys.system_prompt}]
        messages = [{"role": "user", "content": user_prompt}]

        step_name = novelty_embedding_evaluation_step_name(
            evaluator_name,
            context.bullet_index,
        )
        similar_bullets_debug = [
            {"text": row[0], "similarity": round(row[1], 4), "date": row[2].strftime("%Y-%m-%d")}
            for row in previous
        ]

        max_attempts = 3
        last_error: Exception | None = None
        result: NoveltyClassification | None = None
        global _novelty_parse_attempts, _novelty_parse_failures, _novelty_step1_missing_evidence_count
        for attempt in range(max_attempts):
            _novelty_parse_attempts += 1
            try:
                result = self.llm_client.call_with_response_format(
                    system=system,
                    messages=messages,
                    text_format=NoveltyClassification,
                    step_name=step_name,
                    debug_logger=context.debug_logger,
                    entity_metrics=context.entity_metrics,
                    debug_metadata={
                        "evaluator_name": evaluator_name,
                        "similar_previous_bullets": similar_bullets_debug,
                        "mode": "two_step_step1",
                    },
                    **self._step1_keys.llm_kwargs,
                )
                decision = result.decision.upper()
                evidence_ids_raw: list[str] = getattr(result, "evidence_ids", None) or []
                # Validation: REWRITE/DISCARD must have at least one evidence
                if decision in ("REWRITE", "DISCARD") and not evidence_ids_raw:
                    _novelty_step1_missing_evidence_count += 1
                    if STEP1_MISSING_EVIDENCE_RETRIES > 0 and attempt < max_attempts - 1:
                        logger.warning(
                            "[novelty] Step 1 returned %s without evidence_ids (evaluator=%s, bullet_index=%s, attempt=%s, count=%s); retrying.",
                            decision,
                            evaluator_name,
                            context.bullet_index,
                            attempt + 1,
                            _novelty_step1_missing_evidence_count,
                        )
                        continue
                    logger.warning(
                        "[novelty] Step 1 accepted %s without evidence_ids (evaluator=%s, bullet_index=%s, count=%s; retries=%s).",
                        decision,
                        evaluator_name,
                        context.bullet_index,
                        _novelty_step1_missing_evidence_count,
                        STEP1_MISSING_EVIDENCE_RETRIES,
                    )
                    break
                break
            except Exception as e:
                last_error = e
                result = None
                _novelty_parse_failures += 1
                failure_rate = _novelty_parse_failures / _novelty_parse_attempts
                logger.warning(
                    "Novelty Step 1 parse failure (%s/%s, rate=%.2f%%): %s",
                    _novelty_parse_failures,
                    _novelty_parse_attempts,
                    100.0 * failure_rate,
                    e,
                )
                if attempt >= max_attempts - 1:
                    return NoveltyEvaluatorResult(
                        decision="KEEP",
                        reason=f"Error during novelty Step 1 (after {max_attempts} attempts): {last_error!s}",
                        rewritten_text=None,
                        evaluator_name=evaluator_name,
                        evidence_ids=[],
                    )

        if result is None:
            return NoveltyEvaluatorResult(
                decision="KEEP",
                reason="Step 1 produced no result (unexpected).",
                rewritten_text=None,
                evaluator_name=evaluator_name,
                evidence_ids=[],
            )
        decision = result.decision.upper()
        reason = result.reason
        evidence_ids: list[str] = getattr(result, "evidence_ids", None) or []
        instruction: str | None = getattr(result, "instruction", None) or None
        if instruction is not None and isinstance(instruction, str) and not instruction.strip():
            instruction = None

        if decision == "KEEP":
            return NoveltyEvaluatorResult(
                decision="KEEP",
                reason=reason,
                rewritten_text=None,
                evaluator_name=evaluator_name,
                evidence_ids=evidence_ids,
                instruction=None,
            )
        if decision == "REWRITE":
            return NoveltyEvaluatorResult(
                decision="REWRITE",
                reason=reason,
                rewritten_text=None,  # Step 2 fills this after aggregation
                evaluator_name=evaluator_name,
                evidence_ids=evidence_ids,
                instruction=instruction,
            )
        return NoveltyEvaluatorResult(
            decision="DISCARD",
            reason=reason,
            rewritten_text=None,
            evaluator_name=evaluator_name,
            evidence_ids=evidence_ids,
            instruction=None,
        )

    # ------------------------------------------------------------------
    # Two-step mode — Step 2: rewrite (called after aggregation)
    # ------------------------------------------------------------------

    def run_step2_rewrite(
        self,
        original_text: str,
        reviewer_notes: list[str],
        context: NoveltyContext,
        bullet_index: int | None,
    ) -> tuple[str, Literal["REWRITE", "DISCARD"]]:
        """Rewrite the bullet using Step 1 notes (one per REWRITE vote): instruction, or reason if instruction empty.

        The caller logs when reason is used because instruction was missing.

        Returns (rewritten_text, effective_decision).
        effective_decision is "DISCARD" when Step 2 sets is_empty=True (nothing left to keep).
        Falls back to (original_text, "REWRITE") on total LLM failure so the REWRITE decision
        remains visible in the data.
        """
        user_prompt = self._step2_keys.user_template.render(
            new_bulletpoint=original_text,
            reviewer_notes=reviewer_notes,
        )
        system = [{"role": "system", "content": self._step2_keys.system_prompt}]
        messages = [{"role": "user", "content": user_prompt}]

        step_name = novelty_embedding_rewrite_step_name(bullet_index)

        max_attempts = 3
        last_error: Exception | None = None
        global _novelty_step2_parse_attempts, _novelty_step2_parse_failures, _novelty_step2_empty_count
        for attempt in range(max_attempts):
            _novelty_step2_parse_attempts += 1
            try:
                result = self.llm_client.call_with_response_format(
                    system=system,
                    messages=messages,
                    text_format=NoveltyRewrite,
                    step_name=step_name,
                    debug_logger=context.debug_logger,
                    entity_metrics=context.entity_metrics,
                    debug_metadata={
                        "mode": "two_step_step2",
                        "reviewer_notes_count": len(reviewer_notes),
                    },
                    **self._step2_keys.llm_kwargs,
                )
                break
            except Exception as e:
                last_error = e
                _novelty_step2_parse_failures += 1
                failure_rate = _novelty_step2_parse_failures / _novelty_step2_parse_attempts
                logger.warning(
                    "Novelty Step 2 parse failure (%s/%s, rate=%.2f%%): %s",
                    _novelty_step2_parse_failures,
                    _novelty_step2_parse_attempts,
                    100.0 * failure_rate,
                    e,
                )
                if attempt >= max_attempts - 1:
                    _date = context.current_date.strftime("%Y-%m-%d")
                    _model = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
                    _slug = _debug_log_slug(context.entity_name)
                    _failed_file = f"llm_{step_name}_FAILED.json"
                    _debug_path = (
                        f"run_{_model}/debug_logs/{_slug}/{_slug}_{_date}_*/"
                        f"iterative_sequential_with_thematic_chunks/details/04_novelty_check/{_failed_file}"
                    )
                    if context.debug_logger:
                        context.debug_logger.save_llm_failure(
                            step_name=step_name,
                            model=_model,
                            error=last_error or Exception("Unknown"),
                            attempt=max_attempts,
                            user_messages=messages,
                            debug_metadata={"mode": "two_step_step2", "reviewer_notes_count": len(reviewer_notes)},
                        )
                    logger.warning(
                        "[novelty] Step 2 failed after %s attempts | entity=%s date=%s model=%s bullet_index=%s | "
                        "see %s | Error: %s",
                        max_attempts,
                        context.entity_name,
                        _date,
                        _model,
                        bullet_index,
                        _debug_path,
                        last_error,
                    )
                    return original_text, "REWRITE"

        if result is None:
            _date = context.current_date.strftime("%Y-%m-%d")
            _model = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
            _slug = _debug_log_slug(context.entity_name)
            _failed_file = f"llm_{step_name}_FAILED.json"
            _debug_path = (
                f"run_{_model}/debug_logs/{_slug}/{_slug}_{_date}_*/"
                f"iterative_sequential_with_thematic_chunks/details/04_novelty_check/{_failed_file}"
            )
            if context.debug_logger:
                context.debug_logger.save_llm_failure(
                    step_name=step_name,
                    model=_model,
                    error="LLM returned None (no valid NoveltyRewrite parsed)",
                    user_messages=messages,
                    debug_metadata={"mode": "two_step_step2", "reviewer_notes_count": len(reviewer_notes)},
                )
            logger.warning(
                "[novelty] Step 2 returned None | entity=%s date=%s model=%s bullet_index=%s | "
                "see %s",
                context.entity_name,
                _date,
                _model,
                bullet_index,
                _debug_path,
            )
            return original_text, "REWRITE"

        if result.is_empty:
            _novelty_step2_empty_count += 1
            logger.info(
                "[novelty] Step 2 returned is_empty=True for bullet_index=%s (count=%s); "
                "overriding REWRITE → DISCARD.",
                bullet_index,
                _novelty_step2_empty_count,
            )
            return "", "DISCARD"

        rewritten = _strip_double_asterisks(result.rewritten_text) or original_text
        return rewritten, "REWRITE"


# --- Evaluator = Retriever + Judge (one “system”) ---


class NoveltyEvaluator(Protocol):
    """One novelty system: typically Retriever + Judge."""

    @property
    def name(self) -> str:
        ...

    def evaluate(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> NoveltyEvaluatorResult:
        ...


class RetrieverPlusJudgeEvaluator:
    """Composes one Retriever and one Judge; same Judge can be shared across evaluators."""

    def __init__(
        self,
        retriever: NoveltyRetriever,
        judge: LLMNoveltyJudge,
        name: str | None = None,
    ):
        self.retriever = retriever
        self.judge = judge
        self._name = name if name is not None else retriever.name

    @property
    def name(self) -> str:
        return self._name

    def evaluate(
        self,
        bullet_text: str,
        context: NoveltyContext,
    ) -> NoveltyEvaluatorResult:
        retrieved = self.retriever.retrieve(bullet_text, context)
        result = self.judge.judge(
            bullet_text,
            retrieved,
            context,
            evaluator_name=self.name,
        )
        return NoveltyEvaluatorResult(
            decision=result.decision,
            reason=result.reason,
            rewritten_text=_strip_double_asterisks(result.rewritten_text),
            evaluator_name=result.evaluator_name,
            retrieved_bullets=retrieved,
            evidence_ids=result.evidence_ids,
            instruction=getattr(result, "instruction", None),
        )


def make_embedding_llm_evaluator(
    embedding_client: "EmbeddingClient",
    storage: "EmbeddingStorage",
    llm_client: "LLMClient",
    *,
    threshold: float = 0.5,
    top_k: int = 10,
    name: str = "llm",
) -> tuple[RetrieverPlusJudgeEvaluator, LLMNoveltyJudge]:
    """Build the current default evaluator: embedding retriever + LLM judge.

    Returns (evaluator, judge) so the caller can pass the judge to run_step2_rewrite.
    """
    retriever = EmbeddingRetriever(
        embedding_client,
        storage,
        threshold=threshold,
        top_k=top_k,
        name=name,
    )
    judge = LLMNoveltyJudge(llm_client)
    return RetrieverPlusJudgeEvaluator(retriever, judge, name=name), judge


def make_three_window_evaluators(
    embedding_client: "EmbeddingClient",
    storage: "EmbeddingStorage",
    llm_client: "LLMClient",
    *,
    threshold: float = 0.5,
    top_k: int = 10,
) -> tuple[list[RetrieverPlusJudgeEvaluator], LLMNoveltyJudge]:
    """Build three parallel evaluators: novelty window, remaining window, full history.

    All three share the same LLMNoveltyJudge instance.
    Returns (evaluators, judge) so the caller can pass the judge to run_step2_rewrite.
    """
    judge = LLMNoveltyJudge(llm_client)
    evaluators = [
        RetrieverPlusJudgeEvaluator(
            EmbeddingRetriever(
                embedding_client,
                storage,
                threshold=threshold,
                top_k=top_k,
                name="llm_novelty_window",
            ),
            judge,
            name="llm_novelty_window",
        ),
        RetrieverPlusJudgeEvaluator(
            EmbeddingRetrieverRemainingWindow(
                embedding_client,
                storage,
                threshold=threshold,
                top_k=top_k,
                name="llm_remaining_window",
            ),
            judge,
            name="llm_remaining_window",
        ),
        RetrieverPlusJudgeEvaluator(
            EmbeddingRetrieverNoWindow(
                embedding_client,
                storage,
                threshold=threshold,
                top_k=top_k,
                name="llm_full_history",
            ),
            judge,
            name="llm_full_history",
        ),
    ]
    return evaluators, judge

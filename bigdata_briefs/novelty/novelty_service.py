import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, TYPE_CHECKING

import numpy as np

from bigdata_briefs import logger
from bigdata_briefs.metrics import BulletPointMetrics
from bigdata_briefs.models import BulletPointsUsage, RelevanceCheckResult
from bigdata_briefs.novelty.embedding_client import EmbeddingClient
from bigdata_briefs.novelty.evaluators import (
    LLMNoveltyJudge,
    NoveltyContext,
    NoveltyEvaluatorResult,
    _assign_bullet_ids,
    aggregate_evaluator_results,
    make_three_window_evaluators,
)
from bigdata_briefs.novelty.models import BulletPointEmbedding
from bigdata_briefs.novelty.step_names import novelty_embedding_rewrite_relevance_check_step_name
from bigdata_briefs.novelty.storage import EmbeddingStorage
from bigdata_briefs.novelty.wall_timing import (
    NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION,
    NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE,
    NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE_RELEVANCE_CHECK,
    track_novelty_wall_substep,
)
from bigdata_briefs.settings import settings

if TYPE_CHECKING:
    from bigdata_briefs.novelty.evaluators import NoveltyEvaluator
    from bigdata_briefs.debug_logger import DebugLogger
    from bigdata_briefs.llm_client import LLMClient
    from bigdata_briefs.metrics import EntityStepMetrics


def _run_relevance_check_on_rewrite(
    rewritten_text: str,
    entity_name: str,
    entity_ticker: str | None,
    current_datetime_str: str,
    current_quarter_title: str | None,
    bullet_index: int | None,
    llm_client: "LLMClient",
    debug_logger: "DebugLogger | None",
    entity_metrics: "EntityStepMetrics | None",
    *,
    step_name_override: str | None = None,
) -> tuple[int, str | None]:
    """Run a single relevance check LLM call on a rewritten bullet.

    Returns ``(score, reasoning)`` where ``reasoning`` is the LLM's justification.
    On any failure, returns a safe default score (INTRO_SECTION_MIN_RELEVANCE_SCORE + 1)
    so the bullet is kept rather than silently dropped due to an LLM error.
    """
    from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
    default_score = settings.INTRO_SECTION_MIN_RELEVANCE_SCORE + 1
    try:
        relevance_prompt = get_prompt_keys("relevance_check")
        entity_info_str = f"{entity_name} ({entity_ticker})" if entity_ticker else entity_name
        user_content = relevance_prompt.user_template.render(
            entity_info=entity_info_str,
            current_datetime=current_datetime_str,
            contextual_quarter=current_quarter_title or "N/A",
            bullet_text=rewritten_text,
            response_format=f"{RelevanceCheckResult.model_json_schema()}",
        )
        step_name = step_name_override or novelty_embedding_rewrite_relevance_check_step_name(
            bullet_index
        )
        result = llm_client.call_with_response_format(
            system=[{"role": "system", "content": relevance_prompt.system_prompt}],
            messages=[{"role": "user", "content": user_content}],
            text_format=RelevanceCheckResult,
            step_name=step_name,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
            **relevance_prompt.llm_kwargs,
        )
        return result.relevance_score, result.reason
    except Exception as e:
        logger.warning(
            "[novelty] Post-rewrite relevance check failed for bullet_index=%s; "
            "keeping bullet (fail-safe). Error: %s",
            bullet_index,
            e,
        )
        return default_score, None


def run_relevance_check_for_bullet_text(
    rewritten_text: str,
    entity_name: str,
    entity_ticker: str | None,
    current_datetime_str: str,
    current_quarter_title: str | None,
    llm_client: "LLMClient",
    debug_logger: "DebugLogger | None",
    entity_metrics: "EntityStepMetrics | None",
    *,
    step_name: str,
    bullet_index: int | None = None,
) -> tuple[int, str | None]:
    """Run ``relevance_check`` with an explicit ``step_name`` (e.g. Bigdata mixed path)."""
    return _run_relevance_check_on_rewrite(
        rewritten_text,
        entity_name,
        entity_ticker,
        current_datetime_str,
        current_quarter_title,
        bullet_index,
        llm_client,
        debug_logger,
        entity_metrics,
        step_name_override=step_name,
    )


def _step2_notes_from_rewrite_evaluators(
    ev_results: list[NoveltyEvaluatorResult],
    bullet_index: int | None,
    report_date: datetime | None = None,
) -> list[str]:
    """One note per REWRITE vote: removal instruction, or reason if instruction is empty (logged)."""
    notes: list[str] = []
    date_s = report_date.strftime("%Y-%m-%d") if report_date else "?"
    model_s = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
    for r in ev_results:
        if r.decision != "REWRITE":
            continue
        inst = (getattr(r, "instruction", None) or "").strip()
        if inst:
            notes.append(inst)
        else:
            logger.warning(
                "[novelty] REWRITE with empty instruction — using reason for Step 2 "
                "(date=%s, model=%s, evaluator=%s, bullet_index=%s). Step 1 should provide instruction for REWRITE.",
                date_s,
                model_s,
                r.evaluator_name,
                bullet_index,
            )
            reason_text = (r.reason or "").strip()
            if reason_text:
                notes.append(reason_text)
            else:
                logger.error(
                    "[novelty] REWRITE with empty instruction and empty reason "
                    "(date=%s, model=%s, evaluator=%s, bullet_index=%s).",
                    date_s,
                    model_s,
                    r.evaluator_name,
                    bullet_index,
                )
    return notes


def _evaluator_result_to_detail(
    r: NoveltyEvaluatorResult,
) -> dict:
    """Build a serializable detail dict for one evaluator (retrieved bullets + judgment)."""
    detail: dict = {
        "evaluator_name": r.evaluator_name,
        "decision": r.decision,
        "reason": r.reason,
        "rewritten_text": r.rewritten_text,
        "evidence_ids": r.evidence_ids or [],
        "instruction": getattr(r, "instruction", None),
    }
    if r.retrieved_bullets is not None:
        with_ids = _assign_bullet_ids(r.retrieved_bullets)
        evidence_set = set(r.evidence_ids or [])
        detail["retrieved_bullets"] = [
            {
                "id": row[0],
                "text": row[1],
                "score": row[2],
                "date": row[3].isoformat(),
                "evidence": True if row[0] in evidence_set else "",
            }
            for row in with_ids
        ]
    else:
        detail["retrieved_bullets"] = []
    return detail


@dataclass
class LLMNoveltyResult:
    """Result of LLM-based novelty check for a bullet point."""
    original_text: str
    decision: str  # "KEEP", "DISCARD", "REWRITE"
    reason: str
    final_text: str  # original_text if KEEP, rewritten_text if REWRITE, empty if DISCARD
    is_fully_novel: bool
    # Per-evaluator trace: retrieved bullets and LLM judgment for each system (if available)
    evaluator_details: list[dict] | None = None
    # Set to "step2_empty" when Step 2 overrode a REWRITE to DISCARD (is_empty=True),
    # or "step2_relevance" when the rewritten text failed the post-rewrite relevance check.
    # None for all other outcomes. Used to write the correct status to storage.
    discard_source: str | None = None
    # The text produced by Step 2 rewrite before the bullet was discarded.
    # Set only when discard_source is "step2_empty" or "step2_relevance".
    # Stored as pre_rewrite_text in the DB so the app can show what was attempted.
    attempted_rewrite_text: str | None = None


class NoveltyFilteringService:
    def __init__(
        self,
        embedding_client: EmbeddingClient,
        embedding_storage: EmbeddingStorage,
        evaluators: "list[NoveltyEvaluator] | None" = None,
    ):
        self.embedding_client = embedding_client
        self.storage = embedding_storage
        self._evaluators = evaluators

    def novelty_embedding_step(
        self,
        texts: list[str],
        entity_id: str,
        entity_name: str,
        *,
        evaluators: "list[NoveltyEvaluator]",
        start_date: datetime,
        end_date: datetime,
        current_date: datetime,
        clean_up_func: Callable[[str], str] | None = None,
        current_quarter_title: str | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
        judge: LLMNoveltyJudge | None = None,
        llm_client: "LLMClient | None" = None,
        entity_ticker: str | None = None,
        current_datetime_str: str | None = None,
    ) -> tuple[list[str], list[LLMNoveltyResult], list[list[float]], list[BulletPointEmbedding]]:
        """
        Embedding-assisted LLM novelty: multiple evaluators in parallel; veto (any DISCARD)
        and merge REWRITEs; optional Step 2 rewrite + post-rewrite relevance.

        Returns (kept_texts, all_results, all_embeddings, deferred_storage_bullets).

        The fourth element is always an empty list: SQLite rows and embeddings are built
        in ``BriefPipelineService`` after optional ``novelty_search_step`` (single persist).
        """
        # When novelty window already covers full history, skip full_history and remaining_window
        # (remaining has no data before start_date; full_history would duplicate novelty window)
        get_min_date = getattr(self.storage, "get_min_date", None)
        if callable(get_min_date):
            min_stored_date = get_min_date(entity_id)
            if min_stored_date is not None and min_stored_date.replace(tzinfo=min_stored_date.tzinfo or timezone.utc) >= start_date:
                evaluators = [ev for ev in evaluators if ev.name not in ("llm_full_history", "llm_remaining_window")]
                logger.debug(
                    "Novelty window covers full stored history (min_date=%s >= start_date=%s); "
                    "using only novelty_window evaluator",
                    min_stored_date.date(),
                    start_date.date(),
                )

        if not evaluators:
            # No evaluators: keep all; add synthetic evaluator_details so UI can show a trace
            synthetic_detail = [
                {
                    "evaluator_name": "none",
                    "decision": "KEEP",
                    "reason": "No evaluators configured; all bullets kept.",
                    "rewritten_text": None,
                    "evidence_ids": [],
                    "retrieved_bullets": [],
                }
            ]
            results = [
                LLMNoveltyResult(
                    original_text=t,
                    decision="KEEP",
                    reason="No evaluators configured",
                    final_text=t,
                    is_fully_novel=True,
                    evaluator_details=synthetic_detail,
                )
                for t in texts
            ]
            all_emb: list[list[float]] = []
            if texts:
                clean_texts = [clean_up_func(t) if clean_up_func else t for t in texts]
                new_emb = self._compute_embeddings(
                    clean_texts, entity_name=entity_name, entity_metrics=entity_metrics
                )
                all_emb = list(new_emb)
            BulletPointMetrics.track_usage(
                BulletPointsUsage(bullet_points_after_novelty=len(texts))
            )
            return list(texts), results, all_emb, []

        lookback_days = settings.NOVELTY_LOOKBACK_DAYS

        # Precompute embeddings once so all retrievers can use them via context
        clean_texts_for_embedding = [
            clean_up_func(t) if clean_up_func else t for t in texts
        ]
        new_embeddings = self._compute_embeddings(
            clean_texts_for_embedding,
            entity_name=entity_name,
            entity_metrics=entity_metrics,
        )

        # Store one NoveltyContext per bullet index so Step 2 can reuse it (debug_logger, metrics, etc.)
        bullet_contexts: dict[int, NoveltyContext] = {}

        def run_evaluator(
            bullet_idx: int,
            evaluator: "NoveltyEvaluator",
        ) -> tuple[int, str, NoveltyEvaluatorResult]:
            ctx = NoveltyContext(
                entity_id=entity_id,
                entity_name=entity_name,
                start_date=start_date,
                end_date=end_date,
                current_date=current_date,
                lookback_days=lookback_days,
                clean_up_func=clean_up_func,
                current_quarter_title=current_quarter_title,
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                bullet_index=bullet_idx,
                precomputed_embedding=new_embeddings[bullet_idx],
            )
            bullet_contexts[bullet_idx] = ctx
            res = evaluator.evaluate(texts[bullet_idx], ctx)
            return (bullet_idx, evaluator.name, res)

        all_tasks: list[tuple[int, str, NoveltyEvaluatorResult]] = []
        with track_novelty_wall_substep(
            entity_metrics, NOVELTY_WALL_SUBSTEP_EMBEDDING_EVALUATION
        ):
            with ThreadPoolExecutor(max_workers=settings.MAX_NOVELTY_WORKERS) as executor:
                futures = {
                    executor.submit(run_evaluator, idx, ev): (idx, ev.name)
                    for idx in range(len(texts))
                    for ev in evaluators
                }
                for future in as_completed(futures):
                    try:
                        all_tasks.append(future.result())
                    except Exception as e:
                        bidx, ename = futures[future]
                        logger.warning(
                            f"Evaluator {ename} failed for bullet {bidx}: {e}. Discarding bullet."
                        )
                        all_tasks.append(
                            (
                                bidx,
                                ename,
                                NoveltyEvaluatorResult(
                                    decision="DISCARD",
                                    reason=f"Evaluator {ename} failed: {e}",
                                    rewritten_text=None,
                                    evaluator_name=ename,
                                ),
                            )
                        )

        # Group by bullet index
        by_bullet: dict[int, list[NoveltyEvaluatorResult]] = {}
        for bullet_idx, _ename, res in all_tasks:
            by_bullet.setdefault(bullet_idx, []).append(res)

        # Aggregate per bullet and build output
        final_results: list[LLMNoveltyResult] = []
        kept_texts: list[str] = []
        for idx in range(len(texts)):
            ev_results = sorted(
                by_bullet.get(idx, []),
                key=lambda r: r.evaluator_name,
            )
            final_text, decision, combined_reason = aggregate_evaluator_results(
                ev_results, texts[idx]
            )

            # Two-step mode: run Step 2 rewrite when aggregated decision is REWRITE
            discard_source: str | None = None
            attempted_rewrite_text: str | None = None
            if decision == "REWRITE" and judge is not None:
                reviewer_notes = _step2_notes_from_rewrite_evaluators(
                    ev_results, idx, report_date=current_date
                )
                if not reviewer_notes:
                    logger.error(
                        "[novelty] No Step 2 notes for REWRITE (date=%s, model=%s, bullet_index=%s): "
                        "all REWRITE evaluators had empty instruction and empty reason.",
                        current_date.strftime("%Y-%m-%d"),
                        os.environ.get("BRIEFS_DEFAULT_MODEL") or "?",
                        idx,
                    )
                ctx_for_step2 = bullet_contexts.get(idx)
                if ctx_for_step2 is None:
                    # Fallback context if the parallel phase didn't store one (e.g. no evaluators ran)
                    ctx_for_step2 = NoveltyContext(
                        entity_id=entity_id,
                        entity_name=entity_name,
                        start_date=start_date,
                        end_date=end_date,
                        current_date=current_date,
                        lookback_days=lookback_days,
                        clean_up_func=clean_up_func,
                        current_quarter_title=current_quarter_title,
                        debug_logger=debug_logger,
                        entity_metrics=entity_metrics,
                        bullet_index=idx,
                    )
                with track_novelty_wall_substep(
                    entity_metrics, NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE
                ):
                    final_text, decision = judge.run_step2_rewrite(
                        original_text=texts[idx],
                        reviewer_notes=reviewer_notes,
                        context=ctx_for_step2,
                        bullet_index=idx,
                    )
                if decision == "DISCARD":
                    # Step 2 returned is_empty=True — nothing remained after removing known facts.
                    # final_text is "" here; no attempted rewrite to store.
                    discard_source = "step2_empty"
                elif decision == "REWRITE" and llm_client is not None:
                    # Post-rewrite relevance check: the rewritten text may no longer be
                    # relevant enough to include (e.g. all material context was stripped).
                    actual_dt_str = current_datetime_str or (
                        (current_date - timedelta(seconds=1)).strftime("%A, %B %d, %Y")
                    )
                    with track_novelty_wall_substep(
                        entity_metrics,
                        NOVELTY_WALL_SUBSTEP_EMBEDDING_REWRITE_RELEVANCE_CHECK,
                    ):
                        relevance_score = _run_relevance_check_on_rewrite(
                            rewritten_text=final_text,
                            entity_name=entity_name,
                            entity_ticker=entity_ticker,
                            current_datetime_str=actual_dt_str,
                            current_quarter_title=current_quarter_title,
                            bullet_index=idx,
                            llm_client=llm_client,
                            debug_logger=debug_logger,
                            entity_metrics=entity_metrics,
                        )
                    if relevance_score <= settings.INTRO_SECTION_MIN_RELEVANCE_SCORE:
                        logger.info(
                            "[novelty] Post-rewrite relevance check failed for bullet_index=%s "
                            "(score=%s <= %s); overriding REWRITE → DISCARD.",
                            idx,
                            relevance_score,
                            settings.INTRO_SECTION_MIN_RELEVANCE_SCORE,
                        )
                        # Preserve the attempted rewrite so it can be shown in the UI/storage.
                        attempted_rewrite_text = final_text
                        decision = "DISCARD"
                        final_text = ""
                        discard_source = "step2_relevance"

            is_fully_novel = decision != "DISCARD"
            evaluator_details = [_evaluator_result_to_detail(r) for r in ev_results]
            final_results.append(
                LLMNoveltyResult(
                    original_text=texts[idx],
                    decision=decision,
                    reason=combined_reason,
                    final_text=final_text if is_fully_novel else "",
                    is_fully_novel=is_fully_novel,
                    evaluator_details=evaluator_details,
                    discard_source=discard_source,
                    attempted_rewrite_text=attempted_rewrite_text,
                )
            )
            if is_fully_novel:
                kept_texts.append(final_text)

        logger.info(
            f"novelty_embedding_step: {len(kept_texts)} kept, "
            f"{sum(1 for r in final_results if r.decision == 'DISCARD')} discarded, "
            f"{sum(1 for r in final_results if r.decision == 'REWRITE')} rewritten"
        )
        BulletPointMetrics.track_usage(
            BulletPointsUsage(bullet_points_after_novelty=len(kept_texts))
        )
        return kept_texts, final_results, new_embeddings, []

    @staticmethod
    def _calculate_similarity_bp_embedding(
        old_bullet_point_embedding: list[BulletPointEmbedding],
        new_bullet_point_embedding: list[BulletPointEmbedding],
    ):
        old_embedding = np.asarray([bp.embedding for bp in old_bullet_point_embedding])
        new_embedding = np.asarray([bp.embedding for bp in new_bullet_point_embedding])
        return cosine_similarity(old_embedding, new_embedding)

    def _store_embedding(
        self,
        entity_id: str,
        current_embedding_dt: datetime,
        embedding_bp: list[BulletPointEmbedding],
    ):
        # Intentionally no cosine dedup: every row the pipeline hands here is persisted.
        embedding_to_store = embedding_bp

        if embedding_to_store:
            BulletPointMetrics.track_usage(
                BulletPointsUsage(bullet_points_stored=len(embedding_to_store))
            )
            self.storage.store(embedding_to_store)

    def _retrieve_embeddings_from_storage(
        self, entity_id: str, *, start_date: datetime, end_date: datetime
    ) -> list[BulletPointEmbedding]:
        return self.storage.retrieve(
            entity_id, start_date=start_date, end_date=end_date
        )

    def _normalize_text_for_embedding(self, text: str, entity_name: str) -> str:
        """
        Add entity name prefix if not already present.
        
        This improves embedding similarity between bullets about the same entity
        that have different sentence structures (e.g., "The company..." vs "NVIDIA Corp...").
        """
        text_lower = text.lower().strip()
        entity_lower = entity_name.lower()
        
        # Check if entity name is already at the start (with or without markdown bold)
        if text_lower.startswith(entity_lower) or \
           text_lower.startswith(f"**{entity_lower}"):
            return text
        
        # Add entity name prefix
        return f"{entity_name}: {text}"

    def _compute_embeddings(
        self,
        texts: list[str],
        clean_up_func: Callable[[str], str] | None = None,
        entity_name: str | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> list[list[float]]:
        if clean_up_func:
            clean_texts = [clean_up_func(text) for text in texts]
        else:
            clean_texts = texts
        
        # Normalize texts by adding entity name prefix for better similarity matching
        if entity_name:
            clean_texts = [
                self._normalize_text_for_embedding(text, entity_name)
                for text in clean_texts
            ]
        
        return self.embedding_client.compute(clean_texts, entity_metrics=entity_metrics)

    def _prefilter_previous_bullets(
        self,
        new_embedding: list[float],
        prev_bp_embeddings: list[BulletPointEmbedding],
        threshold: float,
        top_k: int,
    ) -> list[tuple[str, float, datetime]]:
        """
        Pre-filter previous bullets: keep top K with similarity >= threshold.
        
        Args:
            new_embedding: Embedding vector for the new bullet point
            prev_bp_embeddings: List of previous bullet point embeddings
            threshold: Minimum similarity to include (e.g., 0.5)
            top_k: Maximum number of previous bullets to include (e.g., 10)
        
        Returns:
            List of tuples (text, similarity_score, date) for filtered previous bullets (most similar first)
        """
        if not prev_bp_embeddings:
            return []
        
        new_emb = np.array(new_embedding)
        
        # Calculate similarities and filter by threshold
        candidates: list[tuple[str, float, datetime]] = []
        for bp in prev_bp_embeddings:
            bp_emb = np.array(bp.embedding)
            # Cosine similarity
            sim = float(np.dot(new_emb, bp_emb) / (np.linalg.norm(new_emb) * np.linalg.norm(bp_emb)))
            if sim >= threshold:
                candidates.append((bp.original_text, sim, bp.date))
        
        # Sort by similarity (highest first) and take top K
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        return candidates[:top_k]

    def filter_by_novelty_llm(
        self,
        texts: list[str],
        entity_id: str,
        entity_name: str,
        *,
        start_date: datetime,
        end_date: datetime,
        current_date: datetime,
        llm_client: "LLMClient",
        clean_up_func: Callable[[str], str] | None = None,
        current_quarter_title: str | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
        entity_ticker: str | None = None,
        current_datetime_str: str | None = None,
    ) -> tuple[list[str], list[LLMNoveltyResult], list[list[float]], list[BulletPointEmbedding]]:
        """
        Filter bullet points by novelty using one or more evaluators (default: LLM).
        Uses embedding-based pre-filtering; delegates to ``novelty_embedding_step``.

        Returns (kept_texts, all_results, all_embeddings, deferred_storage_bullets).
        The fourth list is always empty; persistence is handled in the service layer.
        """
        if self._evaluators:
            # Injected evaluators (e.g. in tests): no shared judge available for Step 2.
            # Step 2 will be skipped unless the caller also injects a judge via novelty_embedding_step.
            evaluators = self._evaluators
            shared_judge: LLMNoveltyJudge | None = None
        else:
            evaluators, shared_judge = make_three_window_evaluators(
                self.embedding_client,
                self.storage,
                llm_client,
                threshold=settings.NOVELTY_PREFILTER_THRESHOLD,
                top_k=settings.NOVELTY_PREFILTER_TOP_K,
            )

        return self.novelty_embedding_step(
            texts=texts,
            entity_id=entity_id,
            entity_name=entity_name,
            evaluators=evaluators,
            start_date=start_date,
            end_date=end_date,
            current_date=current_date,
            clean_up_func=clean_up_func,
            current_quarter_title=current_quarter_title,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
            judge=shared_judge,
            llm_client=llm_client,
            entity_ticker=entity_ticker,
            current_datetime_str=current_datetime_str,
        )


def cosine_similarity(X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """
    Compute cosine similarity between two matrices X and Y

    Based on the implementation in sklearn.metrics.pairwise.cosine_similarity https://github.com/scikit-learn/scikit-learn/blob/main/sklearn/metrics/pairwise.py#L1691-L1748
    """
    dot_product = np.dot(X, Y.T)

    # Compute the norms of the rows of X and Y
    norm_X = np.linalg.norm(X, axis=1).reshape(-1, 1)  # Shape (X, 1)
    norm_Y = np.linalg.norm(Y, axis=1).reshape(1, -1)  # Reshape as (1, Y)

    # Normalize by dividing the dot product by the outer product of the norms
    return dot_product / (norm_X * norm_Y)

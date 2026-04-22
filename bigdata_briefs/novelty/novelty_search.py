"""**Novelty-via-search**: LangGraph archive path (``novelty_via_search`` package)."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar, TypedDict

from bigdata_briefs import logger
from bigdata_briefs.novelty.step_names import novelty_search_evaluation_and_rewrite_step_name

if TYPE_CHECKING:
    from bigdata_briefs.metrics import EntityStepMetrics

_T = TypeVar("_T")


def _run_coroutine_factory(factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run async code from sync callers; safe when a loop is already running (e.g. Jupyter)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    def _worker() -> _T:
        return asyncio.run(factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()


class NoveltyViaSearchUnavailableError(RuntimeError):
    """Raised when novelty-via-search (LangGraph / ``novelty_via_search``) is not importable."""


# Backwards-compatible alias
NoveltySearchUnavailableError = NoveltyViaSearchUnavailableError


def is_novelty_via_search_importable() -> bool:
    try:
        import langgraph  # noqa: F401
        import novelty_via_search.graph  # noqa: F401
    except ImportError:
        return False
    return True


def is_novelty_search_importable() -> bool:
    """Deprecated alias for :func:`is_novelty_via_search_importable`."""
    return is_novelty_via_search_importable()


class _PipelineState(TypedDict, total=False):
    sentence: str
    entity: str
    entity_id: str
    reference_date: str
    use_lookback_window: bool
    lookback_days: int
    num_searches: int
    max_chunks: int
    reranker_threshold: float
    adaptive_threshold: str | None
    enable_relevance_filter: bool
    relevance_batch_size: int | None
    enable_relevance_scoring: bool


def _default_pipeline_state(
    *,
    sentence: str,
    entity_id: str,
    entity_name: str,
    reference_date_iso: str,
) -> _PipelineState:
    from bigdata_briefs.settings import settings

    return {
        "sentence": sentence,
        "entity": entity_name,
        "entity_id": entity_id,
        "reference_date": reference_date_iso,
        "use_lookback_window": False,
        "lookback_days": 30,
        "num_searches": 2,
        "max_chunks": 20,
        "reranker_threshold": 0.5,
        "adaptive_threshold": "searches_adaptive",
        "enable_relevance_filter": True,
        "relevance_batch_size": None,
        # Novelty-via-search graph relevance scoring (not brief-side relevance_check LLM).
        "enable_relevance_scoring": settings.NOVELTY_LANGGRAPH_ENABLE_RELEVANCE_SCORING,
    }


async def _ainvoke_batch_parallel(
    sentences: list[str],
    *,
    entity_id: str,
    entity_name: str,
    reference_date_iso: str,
    max_concurrent: int = 5,
    entity_metrics: EntityStepMetrics | None = None,
    request_hook: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any] | BaseException]:
    """One compiled graph; bounded parallelism; result order matches ``sentences``.

    ``max_concurrent`` caps how many LangGraph invocations run simultaneously
    *per entity*. Each invocation makes several LLM calls internally, so without
    this cap ``MAX_CONCURRENT_ENTITIES * bullets_per_entity * llm_calls_per_bullet``
    concurrent OpenAI requests would hit TPM limits and stall.

    The Bigdata 450 QPM budget is enforced separately via ``request_hook``
    (``RequestsPerMinuteController.aacquire``), which is orthogonal to this cap.

    When ``entity_metrics`` is set, each ``app.ainvoke`` is timed under
    ``novelty_search_evaluation_and_rewrite_{bullet_index}`` (brief-side wall clock only).

    Retry policy (per bullet):
    - Up to ``_MAX_BULLET_RETRIES`` retries on transient errors (connection errors,
      non-timeout exceptions).
    - Each retry compiles a **fresh** LangGraph app so that any poisoned httpcore
      connection pool state from a prior attempt is discarded.
    - ``asyncio.TimeoutError`` is not retried (the bullet is already taking too long).
    - Backoff: 2 s before retry 1, 4 s before retry 2.
    """
    from novelty_via_search.config import config_from_env
    from novelty_via_search.graph import compile_graph
    from bigdata_briefs.settings import settings

    _MAX_BULLET_RETRIES = 2  # up to 3 total attempts per bullet
    _RETRY_BACKOFF_SECONDS = (2.0, 4.0)

    if not sentences:
        return []

    def _make_app():
        """Build a fresh compiled graph with a fresh config (and clean httpcore pools)."""
        cfg = config_from_env()
        if request_hook is not None:
            cfg.set_request_hook(request_hook)
        return compile_graph(cfg)

    sem = asyncio.Semaphore(max(1, max_concurrent))

    async def _one(sentence: str, bullet_idx: int) -> dict[str, Any]:
        state = _default_pipeline_state(
            sentence=sentence,
            entity_id=entity_id,
            entity_name=entity_name,
            reference_date_iso=reference_date_iso,
        )
        step_label = novelty_search_evaluation_and_rewrite_step_name(bullet_idx)
        # Stagger bullet starts to avoid simultaneous TLS handshake bursts to OpenAI.
        # Without this, all bullets acquire the semaphore at once and all attempt to
        # open new httpx connections to api.openai.com in the same instant, which
        # triggers connection errors under concurrent entity load.
        if bullet_idx > 0:
            await asyncio.sleep(bullet_idx * 0.5)
        async with sem:
            logger.info(
                "[novelty_via_search] bullet START",
                bullet_idx=bullet_idx,
                entity_id=entity_id,
                total=len(sentences),
            )
            if entity_metrics is not None:
                entity_metrics.start_step(step_label)
            t0 = asyncio.get_running_loop().time()
            try:
                last_exc: BaseException | None = None
                for attempt in range(_MAX_BULLET_RETRIES + 1):
                    if attempt > 0:
                        backoff = _RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)]
                        logger.info(
                            "[novelty_via_search] bullet RETRY",
                            bullet_idx=bullet_idx,
                            entity_id=entity_id,
                            attempt=attempt,
                            backoff_s=backoff,
                            prev_error=str(last_exc),
                        )
                        await asyncio.sleep(backoff)
                    # Fresh app on every attempt: ensures clean httpcore connection
                    # pools even if a prior attempt left async state corrupted.
                    current_app = _make_app()
                    try:
                        result = await asyncio.wait_for(
                            current_app.ainvoke(state),  # type: ignore[arg-type]
                            timeout=settings.NOVELTY_SEARCH_TIMEOUT_SECONDS,
                        )
                        action = result.get("rewrite_action", "?") if isinstance(result, dict) else "error"
                        logger.info(
                            "[novelty_via_search] bullet DONE",
                            bullet_idx=bullet_idx,
                            entity_id=entity_id,
                            action=action,
                            attempt=attempt,
                            wall_ms=round((asyncio.get_running_loop().time() - t0) * 1000),
                        )
                        return result
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[novelty_via_search] bullet TIMEOUT — skipping after 120s",
                            bullet_idx=bullet_idx,
                            entity_id=entity_id,
                            attempt=attempt,
                        )
                        raise  # no retry on timeout
                    except Exception as exc:
                        last_exc = exc
                        if attempt < _MAX_BULLET_RETRIES:
                            logger.warning(
                                "[novelty_via_search] bullet transient ERROR — will retry",
                                bullet_idx=bullet_idx,
                                entity_id=entity_id,
                                attempt=attempt,
                                error=str(exc),
                            )
                            continue
                        # All attempts exhausted
                        logger.warning(
                            "[novelty_via_search] bullet ERROR — all attempts exhausted",
                            bullet_idx=bullet_idx,
                            entity_id=entity_id,
                            attempts=attempt + 1,
                            error=str(exc),
                        )
                        raise
                # Should never reach here, but satisfy the type checker
                raise RuntimeError("unreachable")  # noqa: TRY301
            finally:
                if entity_metrics is not None:
                    entity_metrics.end_step(step_label)

    return await asyncio.gather(
        *(_one(s, i) for i, s in enumerate(sentences)),
        return_exceptions=True,
    )


def _summarize_novelty_search_results(
    results: list[dict[str, Any] | BaseException],
) -> tuple[dict[str, int], int]:
    verdict_counts: dict[str, int] = {}
    n_errors = 0
    for item in results:
        if isinstance(item, BaseException):
            n_errors += 1
            continue
        if isinstance(item, dict):
            v = str(item.get("overall_verdict", "unknown"))
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        else:
            verdict_counts["non_dict"] = verdict_counts.get("non_dict", 0) + 1
    return verdict_counts, n_errors


def novelty_search_step(
    *,
    sentences: list[str],
    entity_id: str,
    entity_name: str,
    reference_date_iso: str,
    max_concurrent: int = 5,
    entity_metrics: EntityStepMetrics | None = None,
    request_hook: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any] | BaseException]:
    """
    Invoke novelty-via-search (external LangGraph) for many bullets in parallel
    (bounded by ``max_concurrent`` to avoid OpenAI TPM saturation).

    Each list position matches ``sentences``; failures are exception objects, not raised.

    Uses a dedicated thread when an event loop is already running (IPython/Jupyter).

    ``request_hook`` (optional async callable) is forwarded to
    ``_ainvoke_batch_parallel`` and ultimately installed on the compiled
    novelty_via_search ``PipelineConfig`` so every POST inside that graph
    awaits the host's shared 450 QPM rate limiter.
    """
    if not is_novelty_via_search_importable():
        raise NoveltyViaSearchUnavailableError(
            "novelty-via-search (LangGraph) not importable; run ``uv sync`` "
            "(installs the ``novelty-via-search`` distribution)."
        )

    n = len(sentences)
    logger.info(
        "[novelty_via_search] START batch",
        bullets=n,
        entity_id=entity_id,
        entity_name=entity_name,
        reference_date=reference_date_iso,
        max_concurrent=max_concurrent,
    )

    out: list[dict[str, Any] | BaseException] = _run_coroutine_factory(
        lambda: _ainvoke_batch_parallel(
            sentences,
            entity_id=entity_id,
            entity_name=entity_name,
            reference_date_iso=reference_date_iso,
            max_concurrent=max_concurrent,
            entity_metrics=entity_metrics,
            request_hook=request_hook,
        )
    )

    verdict_counts, n_errors = _summarize_novelty_search_results(out)
    logger.info(
        "[novelty_via_search] DONE batch",
        bullets=n,
        entity_id=entity_id,
        verdict_counts=verdict_counts,
        graph_errors=n_errors,
    )

    return out


def run_novelty_search_sync(
    *,
    sentence: str,
    entity_id: str,
    entity_name: str,
    reference_date_iso: str,
    entity_metrics: EntityStepMetrics | None = None,
) -> dict[str, Any]:
    """
    Run novelty-via-search (LangGraph) synchronously for one sentence.

    Requires the ``novelty-via-search`` distribution (``uv sync``).
    """
    results = novelty_search_step(
        sentences=[sentence],
        entity_id=entity_id,
        entity_name=entity_name,
        reference_date_iso=reference_date_iso,
        max_concurrent=1,
        entity_metrics=entity_metrics,
    )
    first = results[0]
    if isinstance(first, dict):
        return first
    if isinstance(first, BaseException):
        raise first
    msg = f"Unexpected novelty_search_step result type: {type(first)!r}"
    raise TypeError(msg)

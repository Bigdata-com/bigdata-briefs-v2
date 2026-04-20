"""
Routes: rate-limit observability

    GET /api/v1/rate/status  → live snapshot of the shared Bigdata QPM budget,
                               connection semaphore and entity worker pool.

Read-only. No auth required on purpose — operators tail this endpoint while
tuning ``MAX_CONCURRENT_ENTITIES`` against the upstream 450 QPM cap.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore

from fastapi import APIRouter, Depends

from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_entity_executor,
    get_rate_limiter,
)
from bigdata_briefs.api.schemas import RateStatusResponse
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings

router = APIRouter(tags=["rate"])


def _semaphore_available(sem: Semaphore) -> int | None:
    """Best-effort introspection of a ``threading.Semaphore``'s remaining slots.

    CPython exposes the count as ``_value`` on the underlying C implementation,
    but this is intentionally private. Fall back to ``None`` when the attribute
    isn't there (e.g. an alternative Python runtime) rather than guessing.
    """
    value = getattr(sem, "_value", None)
    return int(value) if isinstance(value, int) else None


def _executor_in_flight_and_queued(executor: ThreadPoolExecutor) -> tuple[int, int]:
    """Return (in_flight, queued) from a ``ThreadPoolExecutor``'s private state.

    ``_work_queue.qsize()`` counts tasks waiting for a free worker; active
    workers are counted by inspecting ``_threads`` minus idle workers — but
    since that idle count isn't exposed either, we report:

      in_flight = min(len(_threads), max_workers)  minus queue depth seen now
                → approximated as max_workers − remaining idle slots, but the
                  cheapest and most honest proxy is ``_work_queue.qsize()``
                  itself for queued, and ``max_workers`` for running when
                  queue > 0; otherwise we count the threads that exist.

    We return two simple numbers that are monotonically useful for tuning,
    even if they're not exact live gauges.
    """
    work_queue = getattr(executor, "_work_queue", None)
    queued = int(work_queue.qsize()) if work_queue is not None else 0

    threads = getattr(executor, "_threads", None)
    # ``_threads`` is the set of worker threads currently alive; newly spawned
    # workers are added lazily, so this is a floor on concurrency.
    threads_alive = len(threads) if threads is not None else 0

    max_workers = getattr(executor, "_max_workers", settings.MAX_CONCURRENT_ENTITIES)
    # If there are queued tasks, all workers are busy.
    in_flight = min(threads_alive, max_workers)
    if queued > 0:
        in_flight = max_workers

    return in_flight, queued


@router.get(
    "/rate/status",
    response_model=RateStatusResponse,
    summary="Current Bigdata rate-limit budget and worker pool state",
    description=(
        "Snapshot of the process-global rate limiter, connection semaphore, "
        "and parallel entity worker pool.\n\n"
        "Useful for tuning ``MAX_CONCURRENT_ENTITIES`` and spotting when the "
        "upstream 450 QPM cap is the bottleneck versus LLM TPM."
    ),
)
def rate_status(
    rate_limiter: RequestsPerMinuteController = Depends(get_rate_limiter),
    connection_sem: Semaphore = Depends(get_connection_sem),
    executor: ThreadPoolExecutor = Depends(get_entity_executor),
) -> RateStatusResponse:
    # The deque is bounded by ``max_requests_per_refresh`` and each entry is a
    # perf_counter timestamp of an admitted request. Its length is the current
    # window's usage; ``maxlen`` is the window capacity.
    with rate_limiter.lock:
        recent = len(rate_limiter.deque)
        capacity = rate_limiter.deque.maxlen or rate_limiter.max_requests_per_refresh

    in_flight, queued = _executor_in_flight_and_queued(executor)

    return RateStatusResponse(
        queries_in_recent_window=recent,
        window_capacity=int(capacity),
        window_seconds=float(rate_limiter.rate_limit_refresh_frequency),
        connection_sem_capacity=settings.API_SIMULTANEOUS_REQUESTS,
        connection_sem_available=_semaphore_available(connection_sem),
        max_concurrent_entities=settings.MAX_CONCURRENT_ENTITIES,
        entities_in_flight=in_flight,
        entity_queue_depth=queued,
    )

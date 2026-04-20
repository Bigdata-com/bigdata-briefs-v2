"""FastAPI application factory."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Semaphore

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bigdata_briefs import logger
from bigdata_briefs.api.routes.admin import router as admin_router
from bigdata_briefs.api.routes.batch import router as batch_router
from bigdata_briefs.api.routes.entities import router as entities_router
from bigdata_briefs.api.routes.rate import router as rate_router
from bigdata_briefs.api.routes.runs import router as runs_router
from bigdata_briefs.api.routes.universes import router as universes_router
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings


# Bigdata process-wide hard cap. 450 QPM (below the upstream 500 QPM ceiling)
# is enforced here regardless of how many entity pipelines run in parallel.
BIGDATA_MAX_REQUESTS_PER_MINUTE = 450
BIGDATA_RATE_REFRESH_SECONDS = 5
BIGDATA_RATE_RETRY_SECONDS = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build and tear down process-global singletons for the API.

    Every ``APIQueryService`` created by a FastAPI-triggered entity run reads
    these from ``app.state`` (via the getters in ``api.dependencies``) so the
    450 QPM budget, connection pool and HTTP client are shared across all
    concurrent runs. CLI entry points do not go through this path and keep
    per-run locals.
    """
    app.state.bigdata_rate_limiter = RequestsPerMinuteController(
        max_requests_per_min=BIGDATA_MAX_REQUESTS_PER_MINUTE,
        rate_limit_refresh_frequency=BIGDATA_RATE_REFRESH_SECONDS,
        seconds_before_retry=BIGDATA_RATE_RETRY_SECONDS,
    )
    app.state.bigdata_connection_sem = Semaphore(settings.API_SIMULTANEOUS_REQUESTS)
    app.state.bigdata_http_client = httpx.Client(
        base_url=settings.API_BASE_URL,
        headers={
            "X-API-KEY": settings.BIGDATA_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=settings.API_TIMEOUT_SECONDS,
    )
    # Bounded worker pool for ``POST /batch/run-parallel``.  ``max_workers``
    # caps how many entity pipelines run concurrently; the 450 QPM Bigdata
    # budget is enforced independently by ``bigdata_rate_limiter``.
    app.state.entity_executor = ThreadPoolExecutor(
        max_workers=settings.MAX_CONCURRENT_ENTITIES,
        thread_name_prefix="entity-worker",
    )

    logger.info(
        "FastAPI lifespan: Bigdata singletons ready "
        f"(qpm={BIGDATA_MAX_REQUESTS_PER_MINUTE}, "
        f"conn_sem={settings.API_SIMULTANEOUS_REQUESTS}, "
        f"max_entities={settings.MAX_CONCURRENT_ENTITIES})"
    )

    try:
        yield
    finally:
        # Drain the pool first so in-flight entity runs finish cleanly
        # (commit run-log rows, write bullets) before we tear down the
        # HTTP client they depend on. ``wait=True`` is non-negotiable —
        # without it SIGTERM would leave half-written SQLite rows.
        try:
            app.state.entity_executor.shutdown(wait=True, cancel_futures=False)
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.exception("Failed to shut down entity executor")
        try:
            app.state.bigdata_http_client.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.exception("Failed to close shared Bigdata http client")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BigData Briefs Pipeline API",
        description=(
            "Trigger and monitor incremental entity report pipeline runs.\n\n"
            "Authentication: set `PIPELINE_API_KEY` in `.env` and pass the value "
            "as the `X-API-Key` request header. Leave the setting empty to disable auth."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(entities_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(batch_router, prefix="/api/v1")
    app.include_router(rate_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(universes_router, prefix="/api/v1")

    return app


app = create_app()

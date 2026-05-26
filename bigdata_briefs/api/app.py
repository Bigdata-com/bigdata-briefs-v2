"""FastAPI application factory."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Semaphore

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bigdata_briefs import logger
from bigdata_briefs.api.routes.admin import router as admin_router
from bigdata_briefs.api.routes.batch import router as batch_router
from bigdata_briefs.api.routes.entities import router as entities_router
from bigdata_briefs.api.routes.frontend import router as frontend_router, get_data, get_run_data, get_extras, get_portfolio, get_companies_summaries
from bigdata_briefs.api.routes.rate import router as rate_router
from bigdata_briefs.api.routes.report import router as report_router
from bigdata_briefs.api.routes.scan import router as scan_router
from bigdata_briefs.api.routes.runs import router as runs_router
from bigdata_briefs.api.routes.universes import router as universes_router
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings

# Bigdata process-wide hard cap. 450 QPM (below the upstream 500 QPM ceiling)
# is enforced here regardless of how many entity pipelines run in parallel.
BIGDATA_MAX_REQUESTS_PER_MINUTE = 450
BIGDATA_RATE_REFRESH_SECONDS = 5
BIGDATA_RATE_RETRY_SECONDS = 1.0

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DESK_INDEX = _PACKAGE_DIR / "static" / "app" / "index.html"
_DESK_CACHE: dict = {"content": None}


def _build_desk_html() -> str:
    html = _DESK_INDEX.read_text(encoding="utf-8")
    data = get_data()
    data["portfolio"] = get_portfolio().get("portfolio", [])
    # Override companySummaries with the richer version that includes hasRunOnDate
    summaries = get_companies_summaries()
    data["companySummaries"] = summaries.get("summaries", data.get("companySummaries", {}))
    data["lastRunDate"] = summaries.get("date")
    data["publicMode"] = settings.PUBLIC_MODE
    d = json.dumps(data).replace("</", "<\\/")
    script = f"<script>window.DATA={d};window.RUN_DATA={{}};window.EXTRAS={{}};</script>"
    return html.replace("</head>", script + "\n</head>", 1)


def _desk_html() -> str:
    """Return cached desk HTML. Cache is permanent until invalidated by invalidate_desk_cache()."""
    if _DESK_CACHE["content"] is not None:
        return _DESK_CACHE["content"]
    content = _build_desk_html()
    _DESK_CACHE["content"] = content
    return content


def invalidate_desk_cache() -> None:
    """Invalidate the desk HTML cache and pre-warm it in background. Called after a pipeline run completes."""
    _DESK_CACHE["content"] = None
    threading.Thread(target=_desk_html, daemon=True, name="desk-cache-rewarm").start()
    logger.info("Desk cache invalidated and pre-warm started")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    app.state.entity_executor = ThreadPoolExecutor(
        max_workers=settings.MAX_CONCURRENT_ENTITIES,
        thread_name_prefix="entity-worker",
    )

    # Pre-warm desk HTML cache in background so the first user request is instant
    if _DESK_INDEX.exists():
        threading.Thread(target=_desk_html, daemon=True, name="desk-cache-warmup").start()

    logger.info(
        "FastAPI lifespan: singletons ready "
        f"(qpm={BIGDATA_MAX_REQUESTS_PER_MINUTE}, "
        f"conn_sem={settings.API_SIMULTANEOUS_REQUESTS}, "
        f"max_entities={settings.MAX_CONCURRENT_ENTITIES})"
    )

    try:
        yield
    finally:
        try:
            app.state.entity_executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            logger.exception("Failed to shut down entity executor")
        try:
            app.state.bigdata_http_client.close()
        except Exception:
            logger.exception("Failed to close shared Bigdata http client")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bigdata Briefs Pipeline API",
        description=(
            "Trigger and monitor incremental entity report pipeline runs."
        ),
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static files (favicon, htmx, etc.)
    static_dir = _PACKAGE_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # React frontend — served at /app/desk
    # Routes are defined BEFORE the StaticFiles mount so they take priority.
    # The routes inject window.DATA/RUN_DATA/EXTRAS server-side, replacing the
    # three blocking synchronous XHR calls that were in data.js/run-data.js/extras-data.js.
    app_dir = _PACKAGE_DIR / "static" / "app"
    if app_dir.is_dir():
        @app.get("/app/desk", include_in_schema=False)
        @app.get("/app/desk/", include_in_schema=False)
        def app_desk() -> HTMLResponse:
            return HTMLResponse(content=_desk_html())

        app.mount("/app/desk", StaticFiles(directory=str(app_dir)), name="app")

    # Landing pages — served at /landing/* and at /app (product page as entry point)
    landing_dir = _PACKAGE_DIR / "static" / "landing"
    if landing_dir.is_dir():
        app.mount("/landing", StaticFiles(directory=str(landing_dir)), name="landing")

    _product_html = landing_dir / "product.html"

    @app.get("/app", include_in_schema=False)
    async def app_entry() -> HTMLResponse:
        html = _product_html.read_text(encoding="utf-8")
        html = html.replace("<head>", '<head>\n  <base href="/landing/">', 1)
        return HTMLResponse(content=html)

    # Jinja2 templates — shared across all template responses
    templates_dir = _PACKAGE_DIR / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/app/desk")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        return FileResponse(str(static_dir / "favicon.ico"), media_type="image/x-icon")

    @app.get("/favicon.png", include_in_schema=False)
    async def favicon_png() -> FileResponse:
        return FileResponse(str(static_dir / "favicon.png"), media_type="image/png")

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.include_router(entities_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(batch_router, prefix="/api/v1")
    app.include_router(rate_router, prefix="/api/v1", include_in_schema=False)
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(universes_router, prefix="/api/v1")
    app.include_router(report_router, prefix="/api/v1")
    app.include_router(scan_router, prefix="/api/v1")
    app.include_router(frontend_router, prefix="/api/frontend", include_in_schema=False)

    return app


app = create_app()

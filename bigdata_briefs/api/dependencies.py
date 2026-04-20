"""Shared FastAPI dependencies (engine singleton + process-global singletons)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from threading import Semaphore

import httpx
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import create_engine

from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    Return (and cache for the process lifetime) the shared SQLAlchemy engine.

    Configured for multi-threaded SQLite writes:
      * ``check_same_thread=False`` — the same connection may be reused by any
        worker thread (required when many entity runs share one pool).
      * WAL journal mode — readers don't block writers and vice-versa.
      * ``synchronous=NORMAL`` — durable-enough for run-log bookkeeping with
        much lower write latency than ``FULL``.
    """
    eng = create_engine(
        settings.DB_STRING,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cursor.close()

    ensure_orchestration_schema(eng)
    return eng


# ── Process-global singletons (built in FastAPI lifespan, see api/app.py) ──
# Exposed via FastAPI ``Depends(...)`` so routes can read them off
# ``request.app.state`` without every handler touching ``request.app`` directly.


def get_rate_limiter(request: Request) -> RequestsPerMinuteController:
    """Process-global Bigdata QPM limiter (shared across every entity run)."""
    return request.app.state.bigdata_rate_limiter


def get_connection_sem(request: Request) -> Semaphore:
    """Process-global connection semaphore gating concurrent Bigdata sockets."""
    return request.app.state.bigdata_connection_sem


def get_http_client(request: Request) -> httpx.Client:
    """Process-global httpx.Client (thread-safe, shared connection pool)."""
    return request.app.state.bigdata_http_client


def get_entity_executor(request: Request) -> ThreadPoolExecutor:
    """Bounded worker pool used by ``POST /batch/run-parallel``."""
    return request.app.state.entity_executor

"""Self-contained stateless MCP server for bigdata-briefs — stdio transport.

Unlike ``mcp_server.py`` (a thin HTTP client to a running FastAPI app), this server
runs the database-less pipeline **in-process**. There is no separate service to start
and no database: the long-lived MCP process owns one shared rate limiter + worker pool
+ an in-memory job registry, so every run shares a single 450 QPM budget against the
user's own Bigdata key.

Intended distribution model: one MCP process per user, each with its own
BIGDATA_API_KEY / OPENAI_API_KEY. Per-process == per-key, so the rate budget is correct
by construction.

Configuration (env vars or .env):
    BIGDATA_API_KEY   Bigdata.com API key (required).
    OPENAI_API_KEY    OpenAI API key (required).

Tools:
    start_briefs_run(entity_ids|universe, window_start, window_end, categories)
        -> job_id (returns immediately; work runs on the in-process pool)
    get_run_results(job_id)
        -> progress while running, or the formatted briefs when complete
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Semaphore

import httpx
import structlog
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from bigdata_briefs.orchestration.config_load import (
    load_pipeline_config_dict,
    resolve_config_path,
)
from bigdata_briefs.orchestration.stateless_runner import run_entity_stateless
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings

# ── stdio hygiene (must run before any pipeline logging) ───────────────────────
# On an stdio MCP server, stdout IS the JSON-RPC channel. The bigdata_briefs package
# configures structlog to write to stdout, so pipeline log lines would corrupt the
# protocol stream — the client then times out and restarts the server, wiping the
# in-memory job registry. Redirect all logging to stderr to keep stdout clean.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(pad_event=False, colors=False),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    wrapper_class=structlog.make_filtering_bound_logger(
        logging._nameToLevel.get(os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    ),
)
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

load_dotenv()

mcp = FastMCP("briefs-stateless")

# ── Shared in-process singletons (one per MCP process == one per user key) ─────
# Mirror the FastAPI lifespan: a single QPM budget shared across every run.
_BIGDATA_MAX_QPM = 450
_BIGDATA_RATE_REFRESH_SECONDS = 5
_BIGDATA_RATE_RETRY_SECONDS = 1.0
_MINUTES_PER_ENTITY = 2  # rough per-entity estimate for the ETA message
_JOB_TTL_SECONDS = 600  # keep a finished job readable for 10 min, then evict

_rate_limiter = RequestsPerMinuteController(
    max_requests_per_min=_BIGDATA_MAX_QPM,
    rate_limit_refresh_frequency=_BIGDATA_RATE_REFRESH_SECONDS,
    seconds_before_retry=_BIGDATA_RATE_RETRY_SECONDS,
)
_connection_sem = Semaphore(settings.API_SIMULTANEOUS_REQUESTS)
_http_client = httpx.Client(
    base_url=settings.API_BASE_URL,
    headers={"X-API-KEY": str(settings.BIGDATA_API_KEY), "Content-Type": "application/json"},
    timeout=settings.API_TIMEOUT_SECONDS,
)
_executor = ThreadPoolExecutor(
    max_workers=settings.MAX_CONCURRENT_ENTITIES,
    thread_name_prefix="mcp-entity",
)

# job_id -> {status, total, done, progress, results, errors, finished_at, lock}
_jobs: dict[str, dict] = {}
_jobs_lock = Lock()

_VERBATIM_HEADER = "[VERBATIM CONTENT - copy exactly as shown, do not rephrase, translate or summarize]\n"


# ── Universe resolution (CSV only; no DB) ──────────────────────────────────────

_UNIVERSES_DIR = Path(__file__).resolve().parent / "data" / "universes"


def _load_universes() -> dict[str, list[str]]:
    universes: dict[str, list[str]] = {}
    if not _UNIVERSES_DIR.is_dir():
        return universes
    for csv_path in sorted(_UNIVERSES_DIR.glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            universes[csv_path.stem] = [row["id"] for row in reader if row.get("id")]
    return universes


_UNIVERSES = _load_universes()


def _parse_window(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _evict_expired() -> None:
    now = time.monotonic()
    with _jobs_lock:
        stale = [
            jid for jid, e in _jobs.items()
            if e.get("finished_at") is not None and now - e["finished_at"] > _JOB_TTL_SECONDS
        ]
        for jid in stale:
            _jobs.pop(jid, None)


def _format_report(entity_id: str, report: dict) -> str:
    name = report.get("entity_name") or entity_id
    bullets = report.get("bullets") or []
    discarded = report.get("bullets_discarded", 0)

    lines = [f"{name} ({entity_id})", f"{len(bullets)} material developments, {discarded} discarded"]
    if not bullets:
        lines.append("No material developments in this window.")
        return "\n".join(lines)

    for i, bullet in enumerate(bullets, 1):
        # is_novel False = partially novel (mixed): some claim already known in evidence.
        tag = "" if bullet.get("is_novel", True) else "  [partial update]"
        lines.append(f"{i}. {bullet.get('text', '')}{tag}")
        seen: set[str] = set()
        shown = 0
        for c in bullet.get("citations") or []:
            headline = (c.get("headline") or "").strip()
            source = (c.get("source_name") or "").strip()
            url = (c.get("url") or "").strip()
            key = headline or url
            if not key or key in seen:
                continue
            seen.add(key)
            label = " - ".join(p for p in (source, headline) if p) or url
            suffix = f" ({url})" if url else ""
            lines.append(f"   - {label}{suffix}")
            shown += 1
            if shown == 3:
                break
    return "\n".join(lines)


@mcp.tool(name="start_briefs_run")
def start_briefs_run(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    categories: list[str] | None = None,
) -> str:
    """Start the database-less briefs pipeline for a time window. Returns immediately with a job_id.

    The pipeline runs in-process on a bounded worker pool sharing one rate-limit budget.
    After calling this, tell the user the estimated wait, then call get_run_results(job_id).

    Args:
        entity_ids:   List of rp_entity_ids (e.g. ["D8442A", "E09E2B"]). Mutually exclusive with universe.
        universe:     Named CSV universe (e.g. "dow_30", "top_us_100"). Mutually exclusive with entity_ids.
                      Note: "my_portfolio" is not available here (no database) — pass entity_ids instead.
        window_start: ISO 8601 UTC datetime for the window start (e.g. "2026-06-08T12:00:00Z"). Required.
        window_end:   ISO 8601 UTC datetime for the window end (e.g. "2026-06-09T12:00:00Z"). Required.
        categories:   Source categories (e.g. ["news"]). Defaults to pipeline config.

    Returns:
        job_id and estimated wait time. Tell the user to check back with get_run_results(job_id).
    """
    if not window_start or not window_end:
        return "ERROR: window_start and window_end are required (ISO 8601 UTC)."
    if entity_ids and universe:
        return "ERROR: pass either entity_ids or universe, not both."

    if universe:
        if universe == "my_portfolio":
            return (
                "ERROR: 'my_portfolio' is not available in the stateless server (no database). "
                "Pass the explicit entity_ids instead."
            )
        ids = _UNIVERSES.get(universe)
        if ids is None:
            return f"ERROR: universe '{universe}' not found. Available: {sorted(_UNIVERSES)}"
    elif entity_ids:
        ids = list(entity_ids)
    else:
        return "ERROR: provide entity_ids or a universe."

    if not ids:
        return "ERROR: no entity_ids to run."

    try:
        ws = _parse_window(window_start)
        we = _parse_window(window_end)
    except ValueError as e:
        return f"ERROR: invalid window datetime: {e}"
    if we <= ws:
        return "ERROR: window_end must be after window_start."

    cfg = load_pipeline_config_dict(resolve_config_path(None))
    if categories:
        cfg["categories"] = categories

    job_id = str(uuid.uuid4())
    entry = {
        "status": "running",
        "total": len(ids),
        "done": 0,
        "progress": {eid: "queued" for eid in ids},
        "results": {},
        "errors": {},
        "finished_at": None,
        "lock": Lock(),
    }
    with _jobs_lock:
        _jobs[job_id] = entry

    def _one(eid: str):
        def _on_phase(phase: str):
            with entry["lock"]:
                entry["progress"][eid] = phase

        try:
            report = run_entity_stateless(
                entity_id=eid,
                window_start=ws,
                window_end=we,
                pipeline_config=cfg,
                rate_limiter=_rate_limiter,
                connection_sem=_connection_sem,
                http_client=_http_client,
                progress_cb=_on_phase,
            )
            with entry["lock"]:
                entry["results"][eid] = report
                entry["progress"][eid] = "done"
        except Exception as e:  # noqa: BLE001 — per-entity isolation
            with entry["lock"]:
                entry["errors"][eid] = str(e)
                entry["progress"][eid] = "failed"
        finally:
            with entry["lock"]:
                entry["done"] += 1
                if entry["done"] >= entry["total"]:
                    entry["status"] = "finished"
                    entry["finished_at"] = time.monotonic()

    for eid in ids:
        _executor.submit(_one, eid)

    waves = (len(ids) + settings.MAX_CONCURRENT_ENTITIES - 1) // settings.MAX_CONCURRENT_ENTITIES
    eta = waves * _MINUTES_PER_ENTITY
    return (
        f"Run started.\n"
        f"job_id: {job_id}\n"
        f"entities: {len(ids)}\n"
        f"window: {window_start} -> {window_end}\n"
        f"estimated wait: ~{eta} minutes\n\n"
        f"Tell the user to check back in ~{eta} minutes, then call "
        f"get_run_results(job_id='{job_id}')."
    )


@mcp.tool(name="get_run_results")
def get_run_results(job_id: str) -> str:
    """Check a stateless briefs run. Returns progress while running, or the briefs when complete.

    Args:
        job_id: The job_id returned by start_briefs_run.

    Returns:
        If still running: per-entity progress. If complete: the briefs for each entity (verbatim).
    """
    _evict_expired()
    with _jobs_lock:
        entry = _jobs.get(job_id)
    if entry is None:
        return (
            f"ERROR: unknown or expired job_id '{job_id}'. Results are kept for "
            f"{_JOB_TTL_SECONDS // 60} minutes after completion; re-run if expired."
        )

    with entry["lock"]:
        status = entry["status"]
        done = entry["done"]
        total = entry["total"]
        progress = dict(entry["progress"])
        results = dict(entry["results"])
        errors = dict(entry["errors"])

    if status != "finished":
        phase_lines = "\n".join(f"  {eid}: {ph}" for eid, ph in progress.items())
        return (
            f"Still running — {done}/{total} entities complete.\n"
            f"{phase_lines}\n\n"
            f"Check again shortly with get_run_results(job_id='{job_id}')."
        )

    header = (
        f"Completed — {len(results)} succeeded, {len(errors)} failed\n" + "=" * 60 + "\n"
    )
    sections = [_format_report(eid, rep) for eid, rep in results.items()]
    if errors:
        err_lines = "\n".join(f"  {eid}: {msg}" for eid, msg in errors.items())
        sections.append("Failed entities:\n" + err_lines)

    return _VERBATIM_HEADER + header + ("\n" + "=" * 60 + "\n").join(sections)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

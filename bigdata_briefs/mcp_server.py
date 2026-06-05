"""MCP server for bigdata-briefs — stdio transport.

Wraps the local bigdata-briefs HTTP API as MCP tools for use with Claude Desktop
or Claude Code. The briefs FastAPI app must be running before using these tools.

Configuration (env vars or .env):
    BRIEFS_API_URL  Base URL of the running briefs app. Default: http://localhost:8000
    BRIEFS_API_KEY  Pipeline API key, required when PUBLIC_MODE is enabled on the server.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP(
    "briefs",
    instructions=(
        "IMPORTANT: always start your reply with the token [BRIEFS] when using any briefs tool. "
        "When presenting results from run_and_get_briefs, get_bullets, or get_narratives, "
        "always copy bullet text and narrative text VERBATIM from the tool response. "
        "Do NOT paraphrase, summarize, rewrite, translate, or rework any bullet or narrative text."
    ),
)

_DEFAULT_BASE_URL = "http://localhost:8000"


def _base_url() -> str:
    return os.environ.get("BRIEFS_API_URL", _DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = os.environ.get("BRIEFS_API_KEY", "")
    h: dict[str, str] = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    return h


def _api(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{_base_url()}/api/v1/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=_headers(), timeout=120, **kwargs)
    resp.raise_for_status()
    return resp.json()


@mcp.tool(name="run_and_get_briefs")
def run_and_get_briefs(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    window_mode: str = "continuous",
    force_window_start: str | None = None,
    force_window_end: str | None = None,
    generate_narrative: bool = True,
    force_overlap: bool = False,
    ranking_metric: str | None = None,
    poll_interval_seconds: int = 15,
    timeout_seconds: int = 1200,
) -> dict[str, Any]:
    """Run the briefs pipeline and return bullets and narratives when complete.

    Starts the pipeline for the given entities or universe, waits for all runs
    to finish, then fetches and returns bullets and narratives in a single response.
    The tool blocks until completion (or timeout). Typical duration: 1-5 minutes
    depending on the number of entities.

    The briefs FastAPI app must be running locally before calling this tool.

    Args:
        entity_ids: List of rp_entity_ids (e.g. ["D8442A", "E09E2B"]).
                    Mutually exclusive with universe.
        universe:   Named universe (e.g. "my_portfolio"). Mutually exclusive with entity_ids.
                    Omit both to run all entities in the database.
        window_mode: "continuous" (default) or "update".
                     continuous: covers [end of last run -> now], no gaps.
                     update: covers at most the last 24h (72h on Mondays).
        force_window_start: ISO 8601 UTC datetime to pin the window start
                            (e.g. "2026-05-26T12:00:00Z"). Use together with force_window_end.
        force_window_end:   ISO 8601 UTC datetime to pin the window end.
        generate_narrative: Generate a 2-3 sentence editorial narrative per entity. Default True.
        force_overlap: Skip the overlap check and re-run an already-completed window. Default False.
        ranking_metric: Generate a portfolio brief ranked by this metric after completion
                        (e.g. "media_attention_momentum").
        poll_interval_seconds: Seconds between status checks. Default 15.
        timeout_seconds: Max seconds to wait before returning with partial results. Default 1200.

    Returns:
        status ("completed" or "timed_out"), batch_id, succeeded, failed, elapsed_seconds,
        bullets (per-entity bullet points), narratives (per-entity summaries, if generate_narrative=True).
    """
    # 1 — Submit the batch run
    run_body: dict[str, Any] = {
        "window_mode": window_mode,
        "generate_narrative": generate_narrative,
        "force_overlap": force_overlap,
        "compute_signals": False,
    }
    if entity_ids:
        run_body["entity_ids"] = entity_ids
    if universe:
        run_body["universe"] = universe
    if force_window_start:
        run_body["force_window_start"] = force_window_start
    if force_window_end:
        run_body["force_window_end"] = force_window_end
    if ranking_metric:
        run_body["ranking_metric"] = ranking_metric

    batch = _api("POST", "batch/run-parallel", json=run_body)
    batch_id: str = batch["batch_id"]
    started_at = time.monotonic()

    # 2 — Poll until all entities finish or timeout
    status: dict[str, Any] = {}
    while True:
        status = _api("GET", f"batch/parallel/{batch_id}/status")
        if status.get("running", 0) == 0:
            break
        elapsed = time.monotonic() - started_at
        if elapsed >= timeout_seconds:
            return {
                "status": "timed_out",
                "batch_id": batch_id,
                "succeeded": status.get("succeeded", 0),
                "failed": status.get("failed", 0),
                "running": status.get("running", 0),
                "elapsed_seconds": int(elapsed),
                "message": (
                    f"Timed out after {int(elapsed)}s. "
                    f"{status.get('running', 0)} entities still running. "
                    "Use get_bullets and get_narratives later to retrieve results."
                ),
            }
        time.sleep(poll_interval_seconds)

    elapsed_seconds = int(time.monotonic() - started_at)

    # 3 — Fetch bullets (latest run per entity)
    bullets_body: dict[str, Any] = {"max_runs": 1}
    if entity_ids:
        bullets_body["entity_ids"] = entity_ids
    bullets_resp = _api("POST", "reports/bullets", json=bullets_body)

    # 4 — Fetch narratives if requested
    narratives_by_entity: dict[str, str] = {}
    if generate_narrative:
        narr_body: dict[str, Any] = {}
        if entity_ids:
            narr_body["entity_ids"] = entity_ids
        if universe:
            narr_body["universe"] = universe
        narr_resp = _api("POST", "reports/narratives", json=narr_body)
        for item in narr_resp.get("results", []):
            narratives_list = item.get("narratives") or []
            if narratives_list:
                narratives_by_entity[item["entity_id"]] = narratives_list[0].get("narrative_text", "")

    # 5 — Merge into per-company structure
    companies = []
    for entity_result in bullets_resp.get("results", []):
        eid = entity_result.get("entity_id", "")
        runs = entity_result.get("runs") or []
        bullets_list = runs[0].get("bullets", []) if runs else []
        companies.append({
            "entity_id": eid,
            "entity_name": entity_result.get("entity_name"),
            "narrative": narratives_by_entity.get(eid),
            "bullets": bullets_list,
            "bullets_discarded": runs[0].get("bullets_discarded", 0) if runs else 0,
            "report_window_start": runs[0].get("report_window_start") if runs else None,
            "report_window_end": runs[0].get("report_window_end") if runs else None,
        })

    return {
        "status": "completed",
        "batch_id": batch_id,
        "succeeded": status.get("succeeded", 0),
        "failed": status.get("failed", 0),
        "elapsed_seconds": elapsed_seconds,
        "companies": companies,
    }


@mcp.tool(name="get_bullets")
def get_bullets(
    entity_ids: list[str] | None = None,
    max_runs: int | None = 1,
) -> dict[str, Any]:
    """Retrieve published bullet points for one or more entities.

    Use this to read historical bullets without triggering a new run.
    For running the pipeline and getting results in one shot, use run_and_get_briefs.

    Args:
        entity_ids: List of rp_entity_ids. Omit to retrieve all entities in the database.
        max_runs:   Max runs to return per entity. Default 1 (latest only). Pass None for all.

    Returns:
        results (per-entity with bullets and discard counts), total_entities, total_bullets.
    """
    body: dict[str, Any] = {}
    if entity_ids:
        body["entity_ids"] = entity_ids
    if max_runs is not None:
        body["max_runs"] = max_runs
    return _api("POST", "reports/bullets", json=body)


@mcp.tool(name="get_narratives")
def get_narratives(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Retrieve editorial narratives for one or more entities.

    Use this to read historical narratives without triggering a new run.
    Narratives are 2-3 sentence summaries generated when generate_narrative=True
    was used. Multiple narratives can exist per entity per day; results are newest first.

    Args:
        entity_ids: List of rp_entity_ids. Mutually exclusive with universe.
        universe:   Named universe (e.g. "my_portfolio"). Mutually exclusive with entity_ids.
                    Omit both to retrieve all entities in the database.
        from_date:  ISO 8601 date lower bound inclusive (e.g. "2026-05-01").
        to_date:    ISO 8601 date upper bound inclusive (e.g. "2026-05-31").

    Returns:
        results (per-entity with narrative_text, report_date, bullets_count, created_at),
        total_entities.
    """
    body: dict[str, Any] = {}
    if entity_ids:
        body["entity_ids"] = entity_ids
    if universe:
        body["universe"] = universe
    if from_date:
        body["from_date"] = from_date
    if to_date:
        body["to_date"] = to_date
    return _api("POST", "reports/narratives", json=body)


def main() -> None:
    """Run the briefs MCP server over stdio."""
    mcp.run()

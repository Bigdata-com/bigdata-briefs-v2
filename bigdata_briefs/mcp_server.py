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

mcp = FastMCP("briefs")

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


_VERBATIM_HEADER = "[VERBATIM CONTENT - copy exactly as shown, do not rephrase, translate or summarize]\n"


def _format_entity_bullets(entity_result: dict[str, Any], narrative: str | None = None) -> str:
    """Format one entity's bullets and optional narrative as plain text."""
    entity_id = entity_result.get("entity_id", "")
    entity_name = entity_result.get("entity_name") or entity_id
    runs = entity_result.get("runs") or []

    lines: list[str] = []
    lines.append(f"{entity_name} ({entity_id})")

    if not runs:
        lines.append("No runs found.")
        return "\n".join(lines)

    run = runs[0]
    lines.append(f"Window: {run.get('report_window_start')} -> {run.get('report_window_end')}")
    bullets = run.get("bullets") or []
    discarded = run.get("bullets_discarded", 0)
    lines.append(f"{len(bullets)} bullets saved, {discarded} discarded")
    lines.append("")

    if narrative:
        lines.append("Narrative:")
        lines.append(narrative)
        lines.append("")

    if bullets:
        lines.append("Bullets:")
        for i, b in enumerate(bullets, 1):
            lines.append(f"{i}. {b.get('text', '')}")
            seen_sources: set[str] = set()
            unique_citations: list[dict] = []
            for c in (b.get("citations") or []):
                key = c.get("headline", "").strip() or c.get("url", "")
                if key and key not in seen_sources:
                    seen_sources.add(key)
                    unique_citations.append(c)
                if len(unique_citations) == 3:
                    break
            for c in unique_citations:
                headline = c.get("headline", "").strip()
                url = (c.get("url") or "").strip()
                source = (c.get("source_name") or "").strip()
                suffix = url or source
                if suffix:
                    lines.append(f"   - {headline} ({suffix})")
                elif headline:
                    lines.append(f"   - {headline}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool(name="run_and_get_briefs")
def run_and_get_briefs(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    ranking_metric: str | None = None,
    poll_interval_seconds: int = 15,
    timeout_seconds: int = 1200,
) -> str:
    """Run the briefs pipeline for a specific time window and return bullets and narratives.

    Requires an explicit time window (window_start + window_end). Always re-runs
    even if the window was already processed (overlap is always allowed).

    The briefs FastAPI app must be running locally before calling this tool.

    Args:
        entity_ids:   List of rp_entity_ids (e.g. ["D8442A", "E09E2B"]).
                      Mutually exclusive with universe.
        universe:     Named universe (e.g. "my_portfolio"). Mutually exclusive with entity_ids.
                      Omit both to run all entities in the database.
        window_start: ISO 8601 UTC datetime for the window start (e.g. "2026-06-04T12:00:00Z"). Required.
        window_end:   ISO 8601 UTC datetime for the window end (e.g. "2026-06-05T12:00:00Z"). Required.
        ranking_metric: Generate a portfolio brief after completion (e.g. "media_attention_momentum").
        poll_interval_seconds: Seconds between status checks. Default 15.
        timeout_seconds: Max seconds to wait before returning partial results. Default 1200.

    Returns:
        Plain text with bullets and narratives for each entity. Show this output verbatim.
    """
    if not window_start or not window_end:
        return "ERROR: window_start and window_end are required. Provide ISO 8601 UTC datetimes."

    run_body: dict[str, Any] = {
        "force_window_start": window_start,
        "force_window_end": window_end,
        "force_overlap": True,
        "generate_narrative": True,
        "compute_signals": False,
    }
    if entity_ids:
        run_body["entity_ids"] = entity_ids
    if universe:
        run_body["universe"] = universe
    if ranking_metric:
        run_body["ranking_metric"] = ranking_metric

    batch = _api("POST", "batch/run-parallel", json=run_body)
    batch_id: str = batch["batch_id"]
    started_at = time.monotonic()

    status: dict[str, Any] = {}
    while True:
        status = _api("GET", f"batch/parallel/{batch_id}/status")
        if status.get("running", 0) == 0 and status.get("not_started", 0) == 0:
            break
        elapsed = time.monotonic() - started_at
        if elapsed >= timeout_seconds:
            return (
                f"TIMED OUT after {int(elapsed)}s. "
                f"{status.get('running', 0)} entities still running. "
                "Use get_bullets and get_narratives to retrieve results when done."
            )
        time.sleep(poll_interval_seconds)

    elapsed_seconds = int(time.monotonic() - started_at)

    bullets_body: dict[str, Any] = {"max_runs": 1}
    if entity_ids:
        bullets_body["entity_ids"] = entity_ids
    bullets_resp = _api("POST", "reports/bullets", json=bullets_body)

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

    succeeded = status.get("succeeded", 0)
    failed = status.get("failed", 0)
    header = f"Completed in {elapsed_seconds}s — {succeeded} succeeded, {failed} failed\n"
    header += "=" * 60 + "\n"

    sections: list[str] = []
    for entity_result in bullets_resp.get("results", []):
        eid = entity_result.get("entity_id", "")
        narrative = narratives_by_entity.get(eid)
        sections.append(_format_entity_bullets(entity_result, narrative=narrative))

    return _VERBATIM_HEADER + header + ("\n" + "=" * 60 + "\n").join(sections)


@mcp.tool(name="get_bullets")
def get_bullets(
    entity_ids: list[str] | None = None,
    max_runs: int | None = 1,
) -> str:
    """Retrieve published bullet points for one or more entities.

    Use this to read historical bullets without triggering a new run.
    For running the pipeline and getting results in one shot, use run_and_get_briefs.

    Args:
        entity_ids: List of rp_entity_ids. Omit to retrieve all entities in the database.
        max_runs:   Max runs to return per entity. Default 1 (latest only). Pass None for all.

    Returns:
        Plain text with bullets for each entity. Show this output verbatim.
    """
    body: dict[str, Any] = {}
    if entity_ids:
        body["entity_ids"] = entity_ids
    if max_runs is not None:
        body["max_runs"] = max_runs
    resp = _api("POST", "reports/bullets", json=body)

    sections: list[str] = []
    for entity_result in resp.get("results", []):
        sections.append(_format_entity_bullets(entity_result))

    return _VERBATIM_HEADER + (("\n" + "=" * 60 + "\n").join(sections) if sections else "No results found.")


@mcp.tool(name="get_narratives")
def get_narratives(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
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
        Plain text with narratives for each entity. Show this output verbatim.
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
    resp = _api("POST", "reports/narratives", json=body)

    lines: list[str] = []
    for item in resp.get("results", []):
        entity_id = item.get("entity_id", "")
        narratives = item.get("narratives") or []
        if not narratives:
            continue
        lines.append(f"{entity_id}:")
        for n in narratives:
            lines.append(f"  [{n.get('report_date')}] {n.get('narrative_text', '')}")
        lines.append("")

    return "\n".join(lines) if lines else "No narratives found."


def main() -> None:
    """Run the briefs MCP server over stdio."""
    mcp.run()

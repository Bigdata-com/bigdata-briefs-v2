"""MCP server for bigdata-briefs — stdio transport.

Wraps the local bigdata-briefs HTTP API as MCP tools for use with Claude Desktop
or Claude Code. The briefs FastAPI app must be running before using these tools.

Configuration (env vars or .env):
    BRIEFS_API_URL  Base URL of the running briefs app. Default: http://localhost:8000
    BRIEFS_API_KEY  Pipeline API key, required when PUBLIC_MODE is enabled on the server.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("briefs")

_DEFAULT_BASE_URL = "http://localhost:8000"
_MINUTES_PER_ENTITY = 3  # rough estimate for ETA message


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


@mcp.tool(name="start_briefs_run")
def start_briefs_run(
    entity_ids: list[str] | None = None,
    universe: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    ranking_metric: str | None = None,
) -> str:
    """Start the briefs pipeline for a specific time window. Returns immediately with a batch_id.

    After calling this tool, tell the user the estimated wait time and ask them to check
    back by calling get_run_results(batch_id) when ready.

    Args:
        entity_ids:   List of rp_entity_ids (e.g. ["D8442A", "E09E2B"]).
                      Mutually exclusive with universe.
        universe:     Named universe (e.g. "my_portfolio"). Mutually exclusive with entity_ids.
                      Omit both to run all entities in the database.
        window_start: ISO 8601 UTC datetime for the window start (e.g. "2026-06-04T12:00:00Z"). Required.
        window_end:   ISO 8601 UTC datetime for the window end (e.g. "2026-06-05T12:00:00Z"). Required.
        ranking_metric: Generate a portfolio brief after completion (e.g. "media_attention_momentum").

    Returns:
        batch_id and estimated wait time. Tell the user to check back with get_run_results(batch_id).
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
    total: int = batch.get("total", 1)
    eta_minutes = total * _MINUTES_PER_ENTITY

    return (
        f"Run started.\n"
        f"batch_id: {batch_id}\n"
        f"window_start: {window_start}\n"
        f"window_end: {window_end}\n"
        f"entities: {total}\n"
        f"estimated wait: ~{eta_minutes} minutes\n\n"
        f"Tell the user to check back in ~{eta_minutes} minutes. "
        f"Then call get_run_results(batch_id='{batch_id}', window_start='{window_start}', window_end='{window_end}') to retrieve results."
    )


@mcp.tool(name="get_run_results")
def get_run_results(
    batch_id: str,
    window_start: str | None = None,
    window_end: str | None = None,
) -> str:
    """Check the status of a briefs run. Returns results if complete, status if still running.

    Call this after start_briefs_run. Pass window_start and window_end as returned by
    start_briefs_run to ensure the correct run is retrieved (not a concurrent one).

    Args:
        batch_id:     The batch_id returned by start_briefs_run.
        window_start: The window_start returned by start_briefs_run (recommended).
        window_end:   The window_end returned by start_briefs_run (recommended).

    Returns:
        If complete: bullets and narratives for each entity (verbatim).
        If still running: current status with entity counts.
    """
    status = _api("GET", f"batch/parallel/{batch_id}/status")

    running = status.get("running", 0)
    not_started = status.get("not_started", 0)
    succeeded = status.get("succeeded", 0)
    failed = status.get("failed", 0)
    total = status.get("total", 0)

    if running > 0 or not_started > 0:
        done = succeeded + failed
        return (
            f"Still running — {done}/{total} entities complete "
            f"({running} running, {not_started} queued, {failed} failed).\n"
            f"Check again in a few minutes with get_run_results("
            f"batch_id='{batch_id}', window_start='{window_start}', window_end='{window_end}')."
        )

    # All done — fetch bullets and narratives
    runs_list = status.get("runs") or []
    entity_ids_done = [r["entity_id"] for r in runs_list if r.get("status") in ("succeeded", "no_data")]

    bullets_body: dict[str, Any] = {"max_runs": 5}  # fetch last 5 runs to find the right one
    if entity_ids_done:
        bullets_body["entity_ids"] = entity_ids_done
    bullets_resp = _api("POST", "reports/bullets", json=bullets_body)

    # Normalize window timestamps for comparison (strip Z, drop subseconds)
    def _norm(ts: str | None) -> str:
        if not ts:
            return ""
        return ts.rstrip("Z").split(".")[0]

    ws_norm = _norm(window_start)
    we_norm = _norm(window_end)

    narr_body: dict[str, Any] = {}
    if entity_ids_done:
        narr_body["entity_ids"] = entity_ids_done
    narr_resp = _api("POST", "reports/narratives", json=narr_body)

    narratives_by_entity: dict[str, str] = {}
    for item in narr_resp.get("results", []):
        narratives_list = item.get("narratives") or []
        if narratives_list:
            narratives_by_entity[item["entity_id"]] = narratives_list[0].get("narrative_text", "")

    header = f"Completed — {succeeded} succeeded, {failed} failed\n" + "=" * 60 + "\n"
    sections: list[str] = []
    for entity_result in bullets_resp.get("results", []):
        eid = entity_result.get("entity_id", "")

        # If window provided, filter to the matching run
        if ws_norm and we_norm:
            filtered = dict(entity_result)
            matching_runs = [
                r for r in (entity_result.get("runs") or [])
                if _norm(r.get("report_window_start")) == ws_norm
                and _norm(r.get("report_window_end")) == we_norm
            ]
            filtered["runs"] = matching_runs if matching_runs else (entity_result.get("runs") or [])[:1]
            entity_result = filtered

        sections.append(_format_entity_bullets(entity_result, narrative=narratives_by_entity.get(eid)))

    return _VERBATIM_HEADER + header + ("\n" + "=" * 60 + "\n").join(sections)


@mcp.tool(name="get_bullets")
def get_bullets(
    entity_ids: list[str] | None = None,
    max_runs: int | None = 1,
) -> str:
    """Retrieve published bullet points for one or more entities without triggering a new run.

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
    """Retrieve editorial narratives for one or more entities without triggering a new run.

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

# Briefs MCP — LLM Skill Guide

> Ad-hoc brief generation via Claude. The automated daily pipeline runs separately via Docker with cron.

---

## Deployment

| Mode | How to start | Cron | Use case |
|------|-------------|------|---------|
| **Docker with cron** | `docker compose --profile cron up` | Yes, Mon-Fri 12:01 UTC | Automated daily briefs |
| **Docker without cron** | `docker compose up` | No | Ad-hoc runs via MCP |
| **Local (no Docker)** | `uv run uvicorn bigdata_briefs.api.app:app --port 8000` | No | Ad-hoc runs via MCP |

---

## Available Tools

| Tool | Purpose |
|------|---------|
| `start_briefs_run` | Start the pipeline. Returns immediately with batch_id + ETA. |
| `get_run_results` | Check status. Returns bullets + narratives when complete. |
| `get_bullets` | Read existing bullet points without triggering a new run. |
| `get_narratives` | Read existing narrative summaries without triggering a new run. |

---

## Standard Workflow

### Step 1 — Start the run

```python
start_briefs_run(
    entity_ids=["D8442A", "E09E2B"],      # or universe="my_portfolio"
    window_start="2026-06-04T12:00:00Z",
    window_end="2026-06-05T12:00:00Z",
)
```

Returns:
```
Run started.
batch_id: abc123...
window_start: 2026-06-04T12:00:00Z
window_end: 2026-06-05T12:00:00Z
entities: 2
estimated wait: ~6 minutes
```

**Tell the user:** "The run has started. Please check back in ~6 minutes."

### Step 2 — Check results

When the user says "check my run" or similar:

```python
get_run_results(
    batch_id="abc123...",
    window_start="2026-06-04T12:00:00Z",
    window_end="2026-06-05T12:00:00Z",
)
```

- If still running: returns progress (X/N entities complete). Tell the user to wait and check again.
- If complete: returns bullets and narratives verbatim.

Always pass `window_start` and `window_end` to `get_run_results` — this ensures the correct run is returned even if other runs are in progress.

---

## Parameters

### start_briefs_run

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | rp_entity_ids. Mutually exclusive with `universe`. |
| `universe` | None | Named universe (e.g. `"my_portfolio"`). Omit both to run all DB entities. |
| `window_start` | — | ISO 8601 UTC window start. **Required.** |
| `window_end` | — | ISO 8601 UTC window end. **Required.** |
| `ranking_metric` | None | Generate a portfolio brief after completion (e.g. `"media_attention_momentum"`). |

### get_run_results

| Parameter | Description |
|-----------|-------------|
| `batch_id` | The batch_id returned by start_briefs_run. |
| `window_start` | The window_start returned by start_briefs_run. Pass it always. |
| `window_end` | The window_end returned by start_briefs_run. Pass it always. |

---

## Time Zone Conversion

Always convert to UTC before passing window times.

| Time Zone | UTC offset | Example |
|-----------|-----------|---------|
| ET (summer, EDT) | UTC-4 | 8:00 AM EDT = 12:00:00Z |
| ET (winter, EST) | UTC-5 | 8:00 AM EST = 13:00:00Z |

---

## Read-only Tools (no new run)

### get_bullets

```python
get_bullets(entity_ids=["D8442A"], max_runs=1)
```

### get_narratives

```python
get_narratives(
    entity_ids=["D8442A"],      # or universe="my_portfolio"
    from_date="2026-06-01",
    to_date="2026-06-05",
)
```

---

## Output Format

```
Completed — 2 succeeded, 0 failed
============================================================
Visa Inc. (93D207)
Window: 2026-06-04T12:00:00 -> 2026-06-05T12:00:00
4 bullets saved, 11 discarded

Narrative:
Visa teams with Brale to pilot stablecoin settlements...

Bullets:
1. Visa Inc. announced a collaboration with Brale...
   - Visa and Brale Explore Private Stablecoin Settlement (https://investor.visa.com/...)
```

Show this output verbatim — do not rephrase, translate, or summarize.

---

## Claude Desktop Setup

```json
{
  "mcpServers": {
    "briefs": {
      "command": "uv",
      "args": ["--directory", "/path/to/bigdata-briefs-v2", "run", "briefs-mcp"],
      "env": {
        "BRIEFS_API_URL": "http://localhost:8000",
        "BRIEFS_API_KEY": "your-key-here"
      }
    }
  }
}
```

`BRIEFS_API_KEY` can be omitted if the server runs without `PUBLIC_MODE`.

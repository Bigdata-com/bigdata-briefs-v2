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
| `run_and_get_briefs` | Run the pipeline for a time window and return bullets + narratives |
| `get_bullets` | Read existing bullet points without triggering a new run |
| `get_narratives` | Read existing narrative summaries without triggering a new run |

---

## run_and_get_briefs

Runs the pipeline for an explicit time window and returns bullets and narratives when complete. Always re-runs even if the window was already processed. Blocks until done (typically 1-5 minutes per entity).

**Required:** `window_start` and `window_end` — always provide explicit ISO 8601 UTC datetimes.

```python
run_and_get_briefs(
    entity_ids=["D8442A", "E09E2B"],      # or universe="my_portfolio"
    window_start="2026-06-04T12:00:00Z",
    window_end="2026-06-05T12:00:00Z",
    generate_narrative=True,               # default, omit unless user says no
)
```

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | rp_entity_ids to run. Mutually exclusive with `universe`. |
| `universe` | None | Named universe (e.g. `"my_portfolio"`). Omit both to run all DB entities. |
| `window_start` | — | ISO 8601 UTC window start. **Required.** |
| `window_end` | — | ISO 8601 UTC window end. **Required.** |
| `generate_narrative` | `True` | Generate a 2-3 sentence editorial narrative per entity. |
| `ranking_metric` | None | Generate a portfolio brief after completion (e.g. `"media_attention_momentum"`). |
| `poll_interval_seconds` | `15` | Seconds between status checks. |
| `timeout_seconds` | `1200` | Max wait before returning partial results (20 min). |

**Converting time zones:** ET (Eastern Time) is UTC-5 in winter and UTC-4 in summer (EDT). Always convert to UTC before passing.

---

## get_bullets

Reads bullet points already saved in the database. Does not trigger a new run.

```python
get_bullets(
    entity_ids=["D8442A"],
    max_runs=1,    # 1 = latest run only, None = all historical runs
)
```

---

## get_narratives

Reads editorial narratives already saved in the database. Does not trigger a new run.

```python
get_narratives(
    entity_ids=["D8442A"],           # or universe="my_portfolio"
    from_date="2026-06-01",
    to_date="2026-06-05",
)
```

---

## Reading the Output

### run_and_get_briefs result

```
Completed in 87s — 2 succeeded, 0 failed
============================================================
Visa Inc. (93D207)
Window: 2026-06-04T12:00:00 -> 2026-06-05T12:00:00
4 bullets saved, 11 discarded

Narrative:
Visa teams with Brale to pilot stablecoin settlements...

Bullets:
1. Visa Inc. announced a collaboration with Brale...
   - Visa and Brale Explore Private Stablecoin Settlement (https://investor.visa.com/...)
2. ...
```

If `status` is `TIMED OUT`, the run exceeded 20 minutes. Call `get_bullets` and `get_narratives` later to retrieve results.

### Bullet citations

Each bullet shows up to 3 unique sources (deduplicated by headline):
- With URL: `- Headline (https://...)`
- Without URL: `- Headline (Source Name)`

### Narratives

2-3 sentence editorial summary of all bullets for that entity on that day. Only present when `generate_narrative=True`.

---

## Common Patterns

### "Give me the news for Apple in the last 24 hours"

Calculate `window_start = now - 24h` in UTC, `window_end = now` in UTC:

```python
run_and_get_briefs(
    entity_ids=["D8442A"],
    window_start="2026-06-04T14:00:00Z",
    window_end="2026-06-05T14:00:00Z",
)
```

### "Give me briefs for my portfolio from Wednesday 8am to Thursday 8am ET"

Convert ET to UTC (EDT = UTC-4 in summer):

```python
run_and_get_briefs(
    universe="my_portfolio",
    window_start="2026-06-04T12:00:00Z",   # Wed 8am EDT = 12:00 UTC
    window_end="2026-06-05T12:00:00Z",     # Thu 8am EDT = 12:00 UTC
)
```

### "Show me the latest bullets for Visa" (no new run)

```python
get_bullets(entity_ids=["93D207"], max_runs=1)
```

### "Show me narratives for last week" (no new run)

```python
get_narratives(universe="my_portfolio", from_date="2026-05-28", to_date="2026-06-04")
```

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

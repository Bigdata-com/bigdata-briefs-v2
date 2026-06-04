# Briefs MCP — LLM Skill Guide

> Ad-hoc brief generation via Claude. The automated daily pipeline runs separately via Docker with cron.

---

## Two deployment modes

| Mode | How to start | Cron | Use case |
|------|-------------|------|---------|
| **Docker with cron** | `docker compose --profile cron up` | Yes, Mon-Fri 12:01 UTC | Automated daily briefs |
| **Docker without cron** | `docker compose up` | No | Ad-hoc runs via MCP |

When using MCP, always use the second mode. Claude triggers runs explicitly — the cron does not run.

---

## Available Tools

| Tool | Purpose |
|------|---------|
| `run_and_get_briefs` | Run the pipeline and return bullets + narratives when complete |
| `get_bullets` | Read existing bullet points without triggering a new run |
| `get_narratives` | Read existing narrative summaries without triggering a new run |

---

## The Two Run Modes

### Mode 1 — Incremental update (default)

Picks up exactly where the last run ended. Use this when the user asks to update or refresh briefs.

```python
result = run_and_get_briefs(
    entity_ids=["D8442A", "E09E2B"],   # or use universe="my_portfolio"
    window_mode="continuous",           # default, can be omitted
    generate_narrative=True,            # default, can be omitted
)
```

- No overlap with previous runs.
- Falls back to UTC midnight of today if no previous run exists.

### Mode 2 — Custom time window

Use when the user asks for news or briefs covering a specific past period (e.g. "last 36 hours", "last 3 days"). Calculate `force_window_start` and `force_window_end` from the current time and always set `force_overlap=True`.

```python
# "Give me the news for Apple in the last 36 hours"
result = run_and_get_briefs(
    entity_ids=["D8442A"],
    force_window_start="2026-06-03T02:00:00Z",   # now - 36h
    force_window_end="2026-06-04T14:00:00Z",      # now
    force_overlap=True,                            # required: window overlaps previous runs
    generate_narrative=True,
)
```

**Why force_overlap=True:** the requested window almost certainly overlaps runs already in the DB. Without this flag the pipeline would reject the entity immediately.

**Novelty and overlap:** bullets from runs whose `window_end` falls inside the requested window are excluded from the novelty deduplication check. This means bullets that were already reported in an overlapping run can reappear — which is the intended behavior when the user explicitly asks for a time window.

---

## Default Behavior Rules

- **Always use `run_and_get_briefs`** when the user wants to generate or refresh briefs.
- **`generate_narrative` defaults to True.** Only set it to False if the user explicitly says they don't need summaries.
- **Incremental update is the default.** Use a custom window only when the user specifies a time range ("last X hours", "last X days").
- **Always set `force_overlap=True`** when using a custom window.
- **Use `get_bullets` or `get_narratives`** only when the user asks to read existing results without running the pipeline again (e.g. "show me yesterday's briefs").
- If the tool returns a connection error, tell the user to start the app: `docker compose up`.

## Presenting Results — CRITICAL

- **Show bullet text VERBATIM.** Never paraphrase, summarize, or rewrite the bullet text from the tool response. The bullets are already processed and verified by the pipeline — any rewording introduces errors.
- **Show narrative text VERBATIM.** Same rule applies to the narrative field.
- You may add formatting (bold company name, section headers) but the text content must be copied exactly as returned by the tool.
- If the user asks "what does this mean?" or wants interpretation, answer separately — never mix interpretation with the raw bullet text.

---

## Reading the Output

### run_and_get_briefs result structure

```json
{
  "status": "completed",
  "batch_id": "...",
  "succeeded": 2,
  "failed": 0,
  "elapsed_seconds": 87,
  "bullets": { ... },
  "narratives": { ... }
}
```

If `status` is `"timed_out"`, some entities are still running (exceeded 10 min). Tell the user and suggest calling `get_bullets` / `get_narratives` in a few minutes.

If `failed > 0`, report which entities failed and why (visible in `bullets.results`).

### Bullets (per entity)

```json
{
  "entity_id": "D8442A",
  "entity_name": "Apple Inc.",
  "total_bullets": 7,
  "runs": [
    {
      "report_window_start": "2026-06-04T00:00:00",
      "report_window_end":   "2026-06-04T14:30:00",
      "bullet_count": 7,
      "bullets_discarded": 4,
      "bullets": [
        {
          "text": "Apple reported record services revenue of $26.6bn...",
          "citations": [{"headline": "Apple Q2 2026 earnings beat...", "text": "..."}],
          "not_fully_novel": false
        }
      ],
      "discarded_by_relevance": [...],
      "discarded_by_grounding": [...],
      "discarded_by_novelty":   [...]
    }
  ]
}
```

**Discard stages:**
- `discarded_by_relevance`: not financially material enough
- `discarded_by_grounding`: not verifiable against cited sources
- `discarded_by_novelty`: already reported in a previous run (outside the overlapping window)

### Narratives (per entity)

```json
{
  "entity_id": "D8442A",
  "narratives": [
    {
      "report_date": "2026-06-04",
      "narrative_text": "Apple reported strong Q2 results driven by services growth...",
      "bullets_count": 7
    }
  ]
}
```

---

## Parameters Reference

### run_and_get_briefs

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | rp_entity_ids (e.g. `["D8442A"]`). Mutually exclusive with `universe`. |
| `universe` | None | Named universe (e.g. `"my_portfolio"`). Omit both to run all DB entities. |
| `window_mode` | `"continuous"` | `"continuous"`: picks up from last run. `"update"`: last 24h (72h on Mondays). |
| `force_window_start` | None | ISO 8601 UTC. Pin the window start. Use for custom time ranges. |
| `force_window_end` | None | ISO 8601 UTC. Pin the window end. |
| `generate_narrative` | `True` | Generate a 2-3 sentence editorial narrative per entity. |
| `force_overlap` | `False` | Required when using a custom window that overlaps previous runs. |
| `ranking_metric` | None | Generate a portfolio brief after completion (e.g. `"media_attention_momentum"`). |
| `poll_interval_seconds` | `15` | Seconds between status checks. |
| `timeout_seconds` | `600` | Max wait before returning partial results (10 min). |

### get_bullets

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | Omit to retrieve all entities. |
| `max_runs` | `1` | Runs to return per entity. `None` for all historical runs. |

### get_narratives

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | Mutually exclusive with `universe`. |
| `universe` | None | Named universe. Mutually exclusive with `entity_ids`. |
| `from_date` | None | ISO 8601 date lower bound (e.g. `"2026-05-01"`). |
| `to_date` | None | ISO 8601 date upper bound (e.g. `"2026-05-31"`). |

---

## Common Patterns

### "Update my portfolio"
```python
run_and_get_briefs(universe="my_portfolio")
```

### "Update Apple and Microsoft"
```python
run_and_get_briefs(entity_ids=["D8442A", "E09E2B"])
```

### "Give me the news for Apple in the last 36 hours"
```python
run_and_get_briefs(
    entity_ids=["D8442A"],
    force_window_start="<now - 36h, ISO 8601 UTC>",
    force_window_end="<now, ISO 8601 UTC>",
    force_overlap=True,
)
```

### "Show me yesterday's briefs for my portfolio" (no new run)
```python
get_bullets(entity_ids=[...], max_runs=1)
# or
get_narratives(universe="my_portfolio", from_date="2026-06-03", to_date="2026-06-03")
```

---

## Prerequisites

```bash
# Start the app (no cron, for MCP use)
docker compose up

# Or via uv directly
uv run uvicorn bigdata_briefs.api.app:app --port 8000
```

### Claude Desktop config

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

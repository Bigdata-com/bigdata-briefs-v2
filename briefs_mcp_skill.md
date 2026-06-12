# Briefs MCP — LLM Skill Guide

> Generate briefs via MCP. There are **two** servers; pick based on whether you want a
> self-contained run that Claude drives itself, or a locally-running stateful app with
> persisted history.

---

## Choose your mode

| | **Stateless** (`briefs-mcp-stateless`) | **Stateful** (`briefs-mcp`) |
|---|---|---|
| Who runs it | Claude, self-contained (in-process) | You run the FastAPI app; MCP is a thin HTTP client |
| Database | None | SQLite (`briefs.db`) |
| Separate server | No | Yes — the app must be running |
| Persistence | In-memory, ~10 min TTL, lost on restart | Durable; re-readable any time |
| Novelty | Search-only (no history) | Search + embedding history across runs |
| Identifier | `job_id` | `batch_id` |
| Tools | `start_briefs_run`, `get_run_results` | `start_briefs_run`, `get_run_results`, `get_bullets`, `get_narratives` |
| Narratives / portfolio brief | No | Yes (`ranking_metric`, narratives) |
| `my_portfolio` | Not available (pass `entity_ids`) | Available (DB-backed) |
| Keys | `BIGDATA_API_KEY`, `OPENAI_API_KEY` | `BRIEFS_API_URL`, `BRIEFS_API_KEY` |
| Best for | Ad-hoc, one process per user/key, no infra | Running the app locally + leveraging history/UI |

Both convert window times to UTC the same way — see [Time Zone Conversion](#time-zone-conversion-both-modes).

---

# Mode A — Stateless (self-contained, Claude runs it)

> Database-less. The MCP process runs the pipeline in-process, owns one shared 450 QPM
> budget against *your* Bigdata key, and a bounded worker pool. No FastAPI app to start.
> Async by job; results live in RAM with a ~10 min TTL after completion.

## Setup (Claude Desktop / Claude Code)

```json
{
  "mcpServers": {
    "briefs-stateless": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/your/bigdata-briefs-v2", "run", "briefs-mcp-stateless"],
      "env": {
        "BIGDATA_API_KEY": "your-bigdata-key",
        "OPENAI_API_KEY": "your-openai-key"
      }
    }
  }
}
```

Replace the path with the repo location (`pwd` inside the folder). Both keys are required.

## Tools

| Tool | Purpose |
|------|---------|
| `start_briefs_run` | Start the pipeline. Returns immediately with `job_id` + ETA. |
| `get_run_results` | Per-entity progress, or the briefs when complete. |

(DB-backed `get_bullets` / `get_narratives` are **not** available here.)

## Workflow

### Step 1 — Start

```python
start_briefs_run(
    entity_ids=["D8442A", "E09E2B"],      # or universe="dow_30"
    window_start="2026-06-08T12:00:00Z",
    window_end="2026-06-09T12:00:00Z",
    categories=["news"],                  # optional
)
```

Returns:
```
Run started.
job_id: 11aea5f1-...
entities: 2
window: 2026-06-08T12:00:00Z -> 2026-06-09T12:00:00Z
estimated wait: ~2 minutes
```
**Tell the user:** "The run has started. Please check back in ~N minutes."

### Step 2 — Check results

```python
get_run_results(job_id="11aea5f1-...")
```
- Still running: returns per-entity progress (`search → bullet_generation → grounding → novelty → finalizing → done`).
- Complete: returns the briefs verbatim. Readable for ~10 min, then evicted.

## Parameters

### start_briefs_run
| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | rp_entity_ids. Mutually exclusive with `universe`. |
| `universe` | None | CSV universe: `dow_30`, `eurostoxx_50`, `top_us_10/100/500`, `top_eu_100/500`. **`my_portfolio` not available** — pass `entity_ids`. |
| `window_start` | — | ISO 8601 UTC. **Required.** |
| `window_end` | — | ISO 8601 UTC. **Required.** |
| `categories` | config | e.g. `["news"]`. |

### get_run_results
| Parameter | Description |
|-----------|-------------|
| `job_id` | The `job_id` from `start_briefs_run`. |

## Output Format

```
[VERBATIM CONTENT - copy exactly as shown, do not rephrase, translate or summarize]
Completed — 2 succeeded, 0 failed
============================================================
Apple Inc. (D8442A)
3 material developments, 7 discarded
1. Apple raised its FY guidance...
   - Reuters - Apple lifts outlook (https://example.com/...)
2. Services hit record revenue...  [partial update]
   - Bloomberg - Apple Services record (https://example.com/...)
```
- `N material developments, M discarded` — published vs dropped bullets.
- Up to 3 sources per bullet as `source_name - headline (url)`.
- `[partial update]` = not fully novel (`is_fully_novel=false`): at least one claim already known.

## Notes & limits
- **One process per key** — each MCP process uses its own `BIGDATA_API_KEY`, so the 450 QPM budget is correct per key.
- **No persistence** — results in RAM (~10 min TTL), lost on restart. No list-jobs endpoint, so keep the `job_id`.
- **Concurrency** — entities run in parallel up to `MAX_CONCURRENT_ENTITIES`; bigger batches queue (rate-limit back-pressure), not more memory.

---

# Mode B — Stateful (HTTP, you run the app)

> A thin MCP client to a running FastAPI app backed by SQLite. Use when you run the app
> locally and want persisted history, narratives, the web UI, and `my_portfolio`.

## Deployment

| Mode | How to start | Cron | Use case |
|------|-------------|------|---------|
| Docker with cron | `docker compose --profile cron up` | Yes, Mon-Fri 12:01 UTC | Automated daily briefs |
| Docker without cron | `docker compose up` | No | Ad-hoc runs via MCP |
| Local (no Docker) | `uv run uvicorn bigdata_briefs.api.app:app --port 8000` | No | Ad-hoc runs via MCP |

## Setup (Claude Desktop / Claude Code)

```json
{
  "mcpServers": {
    "briefs": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/your/bigdata-briefs-v2", "run", "briefs-mcp"],
      "env": {
        "BRIEFS_API_URL": "http://localhost:8000",
        "BRIEFS_API_KEY": "your-key-here"
      }
    }
  }
}
```
`BRIEFS_API_KEY` can be omitted if the server runs without `PUBLIC_MODE`.

## Tools

| Tool | Purpose |
|------|---------|
| `start_briefs_run` | Start the pipeline. Returns immediately with `batch_id` + ETA. |
| `get_run_results` | Status. Returns bullets + narratives when complete. |
| `get_bullets` | Read existing bullets without a new run. |
| `get_narratives` | Read existing narrative summaries without a new run. |

## Workflow

### Step 1 — Start
```python
start_briefs_run(
    entity_ids=["D8442A", "E09E2B"],      # or universe="my_portfolio"
    window_start="2026-06-04T12:00:00Z",
    window_end="2026-06-05T12:00:00Z",
)
```
Returns `batch_id` + window + ETA.

### Step 2 — Check results
```python
get_run_results(
    batch_id="abc123...",
    window_start="2026-06-04T12:00:00Z",
    window_end="2026-06-05T12:00:00Z",
)
```
Always pass `window_start`/`window_end` so the correct run is returned even if others are in progress.

## Parameters

### start_briefs_run
| Parameter | Default | Description |
|-----------|---------|-------------|
| `entity_ids` | None | rp_entity_ids. Mutually exclusive with `universe`. |
| `universe` | None | Named universe (e.g. `"my_portfolio"`). Omit both to run all DB entities. |
| `window_start` | — | ISO 8601 UTC. **Required.** |
| `window_end` | — | ISO 8601 UTC. **Required.** |
| `ranking_metric` | None | Generate a portfolio brief after completion (e.g. `"media_attention_momentum"`). |

### get_run_results
| Parameter | Description |
|-----------|-------------|
| `batch_id` | The `batch_id` from `start_briefs_run`. |
| `window_start` / `window_end` | Pass the values returned by `start_briefs_run`. |

## Read-only tools (no new run)
```python
get_bullets(entity_ids=["D8442A"], max_runs=1)

get_narratives(
    entity_ids=["D8442A"],      # or universe="my_portfolio"
    from_date="2026-06-01",
    to_date="2026-06-05",
)
```

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

## Time Zone Conversion (both modes)

Always convert to UTC before passing window times.

| Time Zone | UTC offset | Example |
|-----------|-----------|---------|
| ET (summer, EDT) | UTC-4 | 8:00 AM EDT = 12:00:00Z |
| ET (winter, EST) | UTC-5 | 8:00 AM EST = 13:00:00Z |

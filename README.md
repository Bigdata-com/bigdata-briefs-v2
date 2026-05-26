# Bigdata Briefs v2.0

A LangGraph pipeline that generates structured, novelty-filtered brief reports for a universe of companies. For each entity and date window, the service retrieves news evidence from the Bigdata API, extracts material bullet points, and filters them for relevance and novelty before writing them to the database. Results are exposed through a web app and a REST API.

## Architecture overview

![Pipeline diagram](assets/bigdata_briefs_overview.png)

For each entity, the pipeline moves through six sequential phases:

1. **Search**: exploratory pass to discover active themes, fiscal quarter resolution, targeted per-theme retrieval
2. **Bullet Generation**: LLM generates bullets from each theme's evidence, scored for relevance
3. **Grounding Check**: each bullet is validated against its cited source text
4. **Novelty Check via Embedding**: embedding-based retrieval of past bullets, LLM coarse decision
5. **Novelty Check via Search**: claim-level verification against current evidence
6. **Narrative**: multi-sentence editorial summary synthesising all active bullets published that day

For a detailed description of each phase, see the [pipeline reference guide](https://docs.bigdata.com/use-cases/bigdata-briefs-pipeline).

---

## Part 1 — The App

The app is a read-and-run desk available at **`http://localhost:8000/app/desk`**. It is built around **My Portfolio**: a custom list of companies you configure once and then monitor daily. The main navigation has three sections: **The Brief**, **My Portfolio**, and **Costs**.

### Prerequisites

- A **Bigdata.com API key**
- An **OpenAI API key**
- **Docker** (option A) or **uv** (option B)

### Quickstart

#### Option A: Docker

```bash
docker build -t bigdata_briefs .

docker run -d \
  --name bigdata_briefs \
  -p 8000:8000 \
  -e BIGDATA_API_KEY=<your-bigdata-api-key> \
  -e OPENAI_API_KEY=<your-openai-api-key> \
  bigdata_briefs
```

#### Option B: uv (no Docker)

```bash
uv sync
cp .env.example .env
# Edit .env to set BIGDATA_API_KEY and OPENAI_API_KEY
uv run uvicorn bigdata_briefs.api.app:app --host 0.0.0.0 --port 8000
```

Open **`http://localhost:8000/app/desk`** in your browser.

---

### The Brief (default view)

The main reading view of the app. The landing shows the list of portfolio companies sorted by activity: each row displays the company ticker, today's bullet count, and a 7-day mini sparkline of daily output. Clicking a company loads its full brief.

Once a company is selected, a sub-navigation appears with three tabs:

- **Tearsheet** — the full brief for the selected day: narrative, bullets, and right-rail metadata
- **Audit** — every bullet the pipeline considered, both published and discarded, with the reason for each decision
- **Archive** — a calendar of all past brief dates for that company; clicking a date loads that day's tearsheet

**The Tearsheet** contains:
- **Narrative** — an LLM-generated editorial summary of the day's active bullets, shown as the leading paragraph
- **Bullet points** — published bullets grouped by theme, each with inline source citations (publisher + headline + excerpt). Bullets rewritten by the novelty step show a collapsible "Editor's note" explaining what changed.
- **Editor's cut** — visible when enabled via the tweaks panel: discarded bullets grouped by the filter stage that eliminated them (relevance, grounding, novelty), each with the rejection reason
- **Stats bar** — material developments (published bullets), sources scanned, excerpts reviewed, bullets filtered out, and pipeline runtime
- **Date navigation** — prev/next arrows to move between available brief dates

The **right rail** shows:
- **About this brief** — entity metadata: name, ticker, sector, industry, country, entity ID, website
- **14-day pulse** — sparkline of bullets published per day over the past 14 days, with current/average/peak counts
- **Signal history** — media attention sparkline with momentum and z-score metrics vs. 1-month and 1-quarter baselines; sentiment diverging sparkline with its own momentum and z-score metrics
- **Read also** — up to 3 related company briefs from the same date

---

### Portfolio

The Portfolio view is where you build and manage the list of companies the app tracks.

**Adding a company**: use the search bar to find a company by name or ticker. The search covers all entities in the coverage universe — any company that has ever been processed by the pipeline appears here. Select one to add it to the portfolio.

**Removing a company**: click the remove button next to any entry in the portfolio list.

**Running an update**: once your portfolio is set up, click **Start Update** to open the scan/update configuration screen. From there you can select the scope, news sources, and date mode before launching the run. The run uses `window_mode: update` by default — covering at most the last 24 hours from the previous run (72 hours on Mondays to bridge the weekend gap). After the run completes, briefs and narratives for all companies are available in The Brief.

> In `PUBLIC_MODE` the add/remove and run buttons are disabled and show a support contact message instead. Portfolio management and pipeline runs must be done via the API (see Part 2).

---

### History

The History view shows a calendar timeline of all past briefs for any company in the database (not filtered to the portfolio). Companies are listed in the left sidebar, searchable and sortable by most recent activity or alphabetically. Selecting a company shows its full run history grouped by month, with per-day stats: how many bullets were published and how many were discarded. Expanding a day shows the individual runs and their details.

> This view is accessible via the developer tweaks panel at the bottom of the page, not from the main navigation.

---

### Cost

Shows a cost breakdown for the most recent pipeline run: LLM token usage, embedding calls, and Bigdata API calls, grouped by pipeline phase. Useful for understanding which phases dominate cost for a given entity.

---

### Admin

Provides destructive operations with explicit confirmation steps:
- **Reset database** — drops and recreates all tables (requires typing "RESET DATABASE" to confirm)
- **Delete entity** — purges all data for a selected company

Current database statistics (run count, bullet count, entity count) are displayed at the top of the page.

---

## Part 2 — The API

Use the API directly when you want to run the pipeline for entities or universes outside of `my_portfolio`, automate runs from a script or scheduler, backfill historical data, or query results programmatically.

All endpoints live under **`http://localhost:8000/api/v1/`**.

> **Interactive docs** are available at **`http://localhost:8000/docs`** when `ENABLE_DOCS=true` (default).

---

### Run the pipeline

#### `POST /api/v1/batch/run-parallel`

Submits a list of entity IDs (or a named universe) to the pipeline. All entities run concurrently up to the configured worker pool size. Returns a single **batch_id** to monitor progress.

```bash
# Run a list of entities
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "entity_ids": ["0157B1", "D64C6D", "228D42"],
    "force_window_start": "2026-04-22T00:00:00",
    "force_window_end": "2026-04-22T23:59:59"
  }'

# Run an entire pre-defined universe
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "force_window_start": "2026-04-22T00:00:00",
    "force_window_end": "2026-04-22T23:59:59"
  }'
```

> `entity_ids` and `universe` are mutually exclusive. Omit both to run every entity tracked in the database.  
> Omit `force_window_start` / `force_window_end` to use the automatic incremental window (see [Window modes](#window-modes) below).

**What happens automatically after the run:**

- **Per-entity narrative** — as the final step of each entity's pipeline, if at least one bullet was published the LLM generates a 2-3 sentence editorial summary of that entity's bullets for the day. Stored internally and surfaced by the app.
- **Portfolio brief** — once all entities in the batch have finished, a cross-company narrative is generated for the top N companies ranked by the `ranking_metric` parameter (default: `media_attention_momentum`). Only produced if at least one run succeeded.

Both steps are fire-and-forget: a failure in either does not affect the run results or the batch status response.

---

### Monitor a batch

#### `GET /api/v1/batch/parallel/{batch_id}/status`

Returns the real-time status of a batch submitted via `run-parallel`. Reports per-entity counts of `running`, `succeeded`, `failed`, and `not_started`.

```bash
curl http://localhost:8000/api/v1/batch/parallel/3f8a1c2d-.../status
```

#### `GET /api/v1/runs/{run_id}`

Returns the status of a single pipeline run — its window, start/end timestamps, and any error message or exit code if the run failed.

```bash
curl http://localhost:8000/api/v1/runs/3f8a1c2d-...
```

---

### Backfill historical data

#### `POST /api/v1/scan`

Use `scan` when you need to build or backfill a historical record for a portfolio. It takes an explicit date range, splits it into windows, and processes them sequentially.

By default each window spans one UTC calendar day (midnight to midnight). Set `boundary_time` (`HH:MM` UTC) to shift the daily split point — `12:30` gives market-open to market-open windows (08:30 ET). When `boundary_time` is set, Friday windows automatically extend through the weekend to Monday, so each week produces exactly five windows with no weekend gaps.

```bash
# Midnight-to-midnight (default)
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "start_date": "2026-04-01",
    "end_date": "2026-04-30"
  }'

# Market-open to market-open (09:30 ET = 13:30 UTC)
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "start_date": "2026-04-01",
    "end_date": "2026-04-30",
    "boundary_time": "12:30"
  }'
```

Poll progress:

```bash
curl "http://localhost:8000/api/v1/scan/status?entity_ids=D8442A,0157B1&start_date=2026-04-01&end_date=2026-04-30"
```

**Recommended workflow for a new portfolio:**

```bash
# Step 1: build history (optional)
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{"universe": "my_portfolio", "start_date": "2026-04-01", "end_date": "2026-04-30"}'

# Step 2: daily update (run once per day from here on)
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{"universe": "my_portfolio", "window_mode": "daily"}'
```

---

### Retrieve results

The `/reports/` namespace groups all read-only endpoints that query bullet data from the database. These endpoints never trigger any pipeline work — they only read what has already been stored.

#### `POST /api/v1/reports/bullets`

Returns the **published** bullet points for one or more entities, grouped by run. Each bullet includes the final text, source citations (headline, chunk text), and novelty metadata (`search_action`, `not_fully_novel`). Pass an empty `entity_ids` list to retrieve all entities in the database.

The optional `max_runs` parameter controls how many runs per entity are returned (newest first):
- Omit (or `null`) → all runs
- `1` → latest run only
- `N` → last N runs

```bash
# Latest run only for two entities
curl -X POST http://localhost:8000/api/v1/reports/bullets \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1", "D64C6D"], "max_runs": 1}'

# All runs for all entities in the database
curl -X POST http://localhost:8000/api/v1/reports/bullets \
  -H "Content-Type: application/json" \
  -d '{}'
```

#### `POST /api/v1/reports/bullets/detail`

Returns **every bullet considered** by the pipeline — both published and discarded — for one or more entities. For discarded bullets, includes the stage that eliminated them and the specific reason:

- `relevance_score`: scored too low on financial materiality
- `grounding`: text not verifiable against cited sources
- `novelty_embedding`: already reported in a previous run (embedding match)
- `novelty_search`: per-claim verdicts with the evidence chunks that already covered the information

Accepts optional `from_date` and `to_date` filters (ISO 8601) to restrict the date range of runs returned.

```bash
curl -X POST http://localhost:8000/api/v1/reports/bullets/detail \
  -H "Content-Type: application/json" \
  -d '{
    "entity_ids": ["0157B1"],
    "from_date": "2026-04-01T00:00:00",
    "to_date": "2026-04-30T23:59:59"
  }'
```

#### `GET /api/v1/reports/runs/{run_id}/trace`

Returns a **step-by-step trace** of every bullet that passed through the pipeline during a specific run. For each bullet, the trace records:

- `relevance_scoring`: score and reason from the materiality check
- `grounding`: validation decision and reason
- `embedding`: LLM judgment from the embedding novelty step, including similar past bullets found
- `search`: claim-level novelty verdicts from the search novelty step, including any rewrite
- `failure`: error detail if the bullet caused an unexpected exception

This is the most granular view of what the pipeline did and why. Useful for debugging a run or understanding why a specific bullet was discarded or rewritten.

```bash
curl http://localhost:8000/api/v1/reports/runs/3f8a1c2d-.../trace
```

---

### Entity history

#### `GET /api/v1/entities/{entity_id}/runs`

Returns the run history for a single entity — a paginated list of runs with their window, status, timestamps, and any error message. Useful for checking when an entity was last processed and whether previous runs succeeded.

```bash
curl http://localhost:8000/api/v1/entities/0157B1/runs
```

#### `DELETE /api/v1/entities/{entity_id}`

Permanently removes all data for an entity from the database: run logs, bullet points, embeddings, and orchestration state. Returns a breakdown of how many rows were deleted per table.

```bash
curl -X DELETE http://localhost:8000/api/v1/entities/0157B1
```

---

### Universes

#### `GET /api/v1/universes`

Returns all available universe names and their entity counts, including `my_portfolio`.

```bash
curl http://localhost:8000/api/v1/universes
```

#### `GET /api/v1/universes/{name}`

Returns the full list of entity IDs in a named universe.

```bash
curl http://localhost:8000/api/v1/universes/dow_30
```

---

### My portfolio (API)

`my_portfolio` is a special universe stored in the database. Unlike the pre-defined universes (static CSV files), it reflects live state: changes take effect immediately on the next `run-parallel` call. It can be used anywhere a universe name is accepted.

```bash
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{"universe": "my_portfolio", "window_mode": "daily"}'
```

**View the current portfolio:**

```bash
curl http://localhost:8000/api/frontend/portfolio
```

**Add an entity** (name and ticker are resolved automatically from the database if the entity has already been processed):

```bash
curl -X POST http://localhost:8000/api/frontend/portfolio \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "0157B1"}'

# Or supply metadata explicitly:
curl -X POST http://localhost:8000/api/frontend/portfolio \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "0157B1", "entity_name": "Apple Inc.", "kg_ticker": "AAPL"}'
```

**Remove an entity:**

```bash
curl -X DELETE http://localhost:8000/api/frontend/portfolio/0157B1
```

---

### Administration

#### `POST /api/v1/admin/reset-db`

**Drops and recreates all database tables.** All run history, embeddings, and saved bullets are permanently deleted.

```bash
curl -X POST http://localhost:8000/api/v1/admin/reset-db
```

#### `POST /api/v1/admin/clear-stale-runs`

Resets rows stuck in `running` status after a service crash. Rows older than the configured threshold are marked as `failed`.

```bash
curl -X POST http://localhost:8000/api/v1/admin/clear-stale-runs
```

#### `POST /api/v1/admin/delete-date`

Deletes all pipeline runs whose window falls on a specific calendar date. Useful for reprocessing a date from scratch — call this first, then re-submit the same date via `run-parallel`.

```bash
curl -X POST http://localhost:8000/api/v1/admin/delete-date \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-22"}'
```

---

## Window modes

Every run covers a time window `[start, end)`. You can specify it explicitly with `force_window_start` / `force_window_end`, or let the pipeline compute it automatically via `window_mode`.

### `daily` (default)

Covers `[UTC midnight of today → now]`.

- If the pipeline already ran **today**, it resumes from exactly where that run ended.
- If the last run was **yesterday or earlier**, it always resets to midnight of today.

Each day's run is self-contained and deterministic.

### `continuous`

Covers `[end of last run → now]`.

- If the last run was yesterday at 18:00, today's run covers from 18:00 yesterday to now: no gap, no reset.
- If no previous run exists, falls back to `[UTC midnight of today → now]`.

Use this mode when you need a guaranteed gap-free timeline across consecutive runs regardless of when they triggered.

### `update`

Covers at most the **last 24 hours** from the end of the previous run, extended to **72 hours on Mondays** (UTC) to bridge the weekend gap. If no previous run exists, covers the full lookback window from now.

This is the mode used by the app's built-in update button. It is well suited for daily monitoring where you always want to capture the most recent 24 hours without worrying about gaps or resets.

| | `daily` | `continuous` | `update` |
|---|---|---|---|
| No previous run | `[today midnight → now]` | `[today midnight → now]` | `[now − 24h → now]` |
| Last run was today at 09:00 | `[09:00 → now]` | `[09:00 → now]` | `[09:00 → now]` |
| Last run was yesterday at 18:00 | `[today midnight → now]` | `[yesterday 18:00 → now]` | `[yesterday 18:00 → now]` |
| Last run was 3 days ago | `[today midnight → now]` | `[3 days ago end → now]` | `[now − 24h → now]` |

> **Overlap protection**: if the requested window overlaps any already-completed run for the same entity, that entity's run is rejected immediately and marked as `failed`. No API or LLM calls are made.

---

## Pre-defined universes

| Universe | Entities | Description |
|---|---|---|
| `dow_30` | 30 | Dow Jones Industrial Average components |
| `eurostoxx_50` | 50 | Euro Stoxx 50 components |
| `top_us_10` | 10 | Ten largest US listings by market cap |
| `top_us_100` | 100 | Top 100 US companies by market cap |
| `top_us_500` | 500 | Top 500 US companies by market cap |
| `top_eu_100` | 100 | Top 100 European companies by market cap |
| `top_eu_500` | 500 | Top 500 European companies by market cap |
| `my_portfolio` | dynamic | Your custom portfolio — managed via the app or API, stored in the database |

---

## Configuration reference

| Environment variable | Description | Default |
|---|---|---|
| `BIGDATA_API_KEY` | Bigdata.com API key **(required)** | — |
| `OPENAI_API_KEY` | OpenAI API key **(required)** | — |
| `MAX_CONCURRENT_ENTITIES` | Max entities running in parallel | `10` |
| `DB_STRING` | SQLite connection string | `sqlite:///briefs.db` |
| `LLM_TIMEOUT_SECONDS` | LLM call timeout | `60` |
| `NOVELTY_LOOKBACK_DAYS` | Days of history used for novelty checks | `30` |
| `PIPELINE_API_KEY` | When set, all write endpoints require this key in the `X-API-Key` header | — |
| `PUBLIC_MODE` | When `true`, disables write actions in the UI (run, portfolio add/remove) and prevents the API key from being sent to the browser. Intended for shared or external deployments. Direct API calls with a valid `PIPELINE_API_KEY` still work. | `false` |
| `ENABLE_DOCS` | When `true`, exposes `/docs`, `/redoc`, and `/openapi.json` | `true` |

See `.env.example` for the full list with descriptions.

---

## Troubleshooting

**Service not responding**
```bash
docker logs bigdata_briefs
curl http://localhost:8000/health
```

**Entity stuck in `running` for a long time**  
Call `POST /api/v1/admin/clear-stale-runs` to reset it, then re-submit the entity.

**All bullets discarded**  
Expected when the entity has no materially new information in the requested window relative to prior runs. Try a different date range or run on a day with more news activity for that entity.

**Need to reprocess a specific date**  
Call `POST /api/v1/admin/delete-date` with the target date, then re-submit via `run-parallel` with `force_window_start` / `force_window_end` set to that day.

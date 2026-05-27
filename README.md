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

The main reading view of the app. The landing is laid out as follows:

- **Left — Company picker**: a table listing all portfolio companies with three columns: ticker, company name, and today's bullet count ("Items"). Clicking a row loads that company's brief.
- **Right — Portfolio Brief**: shows the top 5 companies ranked by media attention momentum. A toggle switches between two views: **Bullet Points** shows the first 3 published bullets per company; **Summary** shows the LLM-generated narrative per company. A stats strip shows companies run, total material developments, and active names.
- **Below — Upcoming events**: a calendar strip of upcoming earnings calls and conferences for portfolio companies, grouped by day.

Clicking a company opens the **Tearsheet**, which contains:
- **Narrative** — an LLM-generated editorial summary of the day's active bullets, shown as the leading paragraph
- **Bullet points** — published bullets grouped by theme, each with inline source citations (publisher + headline + excerpt). Bullets rewritten by the novelty step show a collapsible "Editor's note" explaining what changed.
- **Stats bar** — material developments (published bullets), sources scanned, excerpts reviewed, bullets filtered out, and pipeline runtime
- **Date navigation** — prev/next arrows to move between available brief dates

The **right rail** shows:
- **About this brief** — entity metadata: name, ticker, sector, industry, country, entity ID, website
- **14-day pulse** — sparkline of bullets published per day over the past 14 days, with current/average/peak counts
- **Signal history** — media attention sparkline with momentum and z-score metrics vs. 1-month and 1-quarter baselines; sentiment diverging sparkline with its own momentum and z-score metrics

Two additional tabs are accessible from the top sub-navigation:
- **Audit** — every bullet the pipeline considered, both published and discarded, with the reason for each decision
- **Archive** — a calendar of all past brief dates for that company; clicking a date loads that day's tearsheet

---

### Portfolio

The Portfolio view is where you build and manage the list of companies the app tracks.

**Adding a company**: use the search bar to find a company by name or ticker. The search covers all entities in the coverage universe — any company that has ever been processed by the pipeline appears here. Select one to add it to the portfolio.

**Removing a company**: click the remove button next to any entry in the portfolio list.

**Running an update**: once your portfolio is set up, click **Start Update** to open the scan/update configuration screen. From there you can select the scope, news sources, and date mode before launching the run. The run uses `window_mode: update` by default — covering at most the last 24 hours from the previous run (72 hours on Mondays to bridge the weekend gap). After the run completes, briefs and narratives for all companies are available in The Brief.

> In `PUBLIC_MODE` the add/remove and run buttons are disabled. Portfolio management and pipeline runs must be done via the API (see Part 2).

---

### Costs

A **Cost forensics** view for a single pipeline run. Select a company and run from the left sidebar to see a breakdown of four cost tiles: **Compute tokens cost**, **Embeddings cost**, **Grounding tokens cost**, and **Total**. Below the tiles, costs are broken down by pipeline phase, showing the relative weight of LLM calls, embeddings, and grounding tokens at each stage.

---

### Scheduled runs (cron job)

When the app runs inside Docker, a cron job starts automatically alongside the server. It is managed by [supercronic](https://github.com/aptible/supercronic) and defined in `crontab`:

```
1 12 * * 1-5  /code/run_daily.sh
```

This triggers `run_daily.sh` every weekday (Monday–Friday) at **12:01 UTC (08:01 ET)**, which calls the `run-parallel` endpoint for the `my_portfolio` universe. No manual action is needed — the pipeline runs on its own and the app updates automatically when you open it.

`run_daily.sh` computes the window automatically:

- On **Monday** the window covers **Friday 12:00 → Monday 12:00 UTC (08:00 ET)** (72 h) to bridge the weekend gap.
- On all other weekdays the window covers **yesterday 12:00 → today 12:00 UTC (08:00 ET)** (24 h).

To change the schedule, edit `crontab` (standard cron expression). To change the universe, window, or whether a portfolio brief is generated, edit `run_daily.sh`. The current payload:

```json
{
  "universe": "my_portfolio",
  "force_window_start": "<computed>",
  "force_window_end": "<computed>",
  "categories": ["news"],
  "generate_narrative": true,
  "ranking_metric": "media_attention_momentum"
}
```

---

## Part 2 — The API

Use the API directly when you want to run the pipeline for entities or universes outside of `my_portfolio`.

All endpoints live under **`http://localhost:8000/api/v1/`**.

> **Interactive docs** are available at **`http://localhost:8000/docs`** when `ENABLE_DOCS=true` (default).

---

### Run the pipeline

#### `POST /api/v1/batch/run-parallel`

Submits a list of entity IDs (or a named universe) to the pipeline. All entities run concurrently up to the configured worker pool size. Returns a single **batch_id** to monitor progress.

**Request body parameters:**

| Parameter | Default | Description |
|---|---|---|
| `entity_ids` | `[]` | List of entity IDs to run. Mutually exclusive with `universe`. Omit both to run all entities in the database. |
| `universe` | `null` | Named universe to run (e.g. `dow_30`, `my_portfolio`). Mutually exclusive with `entity_ids`. |
| `force_window_start` | `null` | Override window start (ISO 8601 UTC). Must be paired with `force_window_end`. |
| `force_window_end` | `null` | Override window end (ISO 8601 UTC). Must be paired with `force_window_start`. |
| `window_mode` | `daily` | How to compute the window when no forced dates are provided. See [Window modes](#window-modes). |
| `categories` | `null` | Source categories to search: `news`, `news_premium`, `filings`, `transcripts`. Defaults to pipeline config (`news`). |
| `generate_narrative` | `false` | When `true`, generates a 2-3 sentence editorial summary per entity after each run. The summary covers **all active bullets for that entity on the same UTC calendar day** — not just bullets from the current run. Retrievable via `POST /api/v1/reports/narratives`. |
| `ranking_metric` | `null` | When set, generates a portfolio brief for the top 5 companies after all entities finish. Available values: `media_attention_momentum` (latest `chunks_momentum_pct`), `media_attention` (\|Δ `chunks_zscore_mo`\|), `sentiment` (\|Δ `sent_zscore_mo`\|). |

```bash
# Minimal: run a list of entities for a specific day
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "entity_ids": ["0157B1", "D64C6D", "228D42"],
    "force_window_start": "2026-04-22T00:00:00",
    "force_window_end": "2026-04-22T23:59:59"
  }'

# Full: run a universe with narrative and portfolio brief
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "force_window_start": "2026-04-22T00:00:00",
    "force_window_end": "2026-04-22T23:59:59",
    "generate_narrative": true,
    "ranking_metric": "media_attention_momentum"
  }'
```

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

#### `POST /api/v1/reports/narratives`

Returns the per-entity editorial narratives generated after pipeline runs. Each narrative is a 2-3 sentence summary of all active bullets published for that entity on the same UTC calendar day. Only available when `generate_narrative: true` was passed to `run-parallel`.

Results are sorted newest first. If an entity was run multiple times on the same day, each run produces its own row — the first entry for a given date is the most up-to-date summary (it accumulates all bullets published so far that day).

```bash
# All entities, last 30 days
curl -X POST http://localhost:8000/api/v1/reports/narratives \
  -H "Content-Type: application/json" \
  -d '{"from_date": "2026-04-27T00:00:00"}'

# Specific entities
curl -X POST http://localhost:8000/api/v1/reports/narratives \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1", "D64C6D"], "from_date": "2026-04-27T00:00:00"}'

# By universe
curl -X POST http://localhost:8000/api/v1/reports/narratives \
  -H "Content-Type: application/json" \
  -d '{"universe": "my_portfolio", "from_date": "2026-04-27T00:00:00"}'
```

---

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

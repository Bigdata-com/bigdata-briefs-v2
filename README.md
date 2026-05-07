# Bigdata Briefs v2.0

A LangGraph pipeline that generates structured, novelty-filtered brief reports for a universe of companies. For each entity and date window, the service retrieves news evidence from the Bigdata API, extracts material bullet points, and filters them for relevance and novelty before writing them to the database. Results are exposed through a REST API.

## Architecture overview

![Pipeline diagram](assets/bigdata_briefs_overview.png)

For each entity, the pipeline moves through six sequential phases:

1. **Search**: exploratory pass to discover active themes, fiscal quarter resolution, targeted per-theme retrieval
2. **Bullet Generation**: LLM generates bullets from each theme's evidence, scored for relevance
3. **Grounding Check**: each bullet is validated against its cited source text
4. **Novelty Check via Embedding**: embedding-based retrieval of past bullets, LLM coarse decision
5. **Novelty Check via Search**: claim-level verification against current evidence
6. **Narrative**: one-sentence editorial summary generated from all active bullets published that day

For a detailed description of each phase, see the [pipeline reference guide](https://docs.bigdata.com/use-cases/bigdata-briefs-pipeline).

## Prerequisites

- A **Bigdata.com API key**
- An **OpenAI API key**
- **Docker** (option A) or **uv** (option B)

## Quickstart

### Option A: Docker

```bash
# Build
docker build -t bigdata_briefs .

# Run
docker run -d \
  --name bigdata_briefs \
  -p 8000:8000 \
  -e BIGDATA_API_KEY=<your-bigdata-api-key> \
  -e OPENAI_API_KEY=<your-openai-api-key> \
  bigdata_briefs
```

### Option B: uv (no Docker)

```bash
# Install uv if needed: https://docs.astral.sh/uv/getting-started/installation/

uv sync

cp .env.example .env
# Edit .env to set BIGDATA_API_KEY and OPENAI_API_KEY

uv run uvicorn bigdata_briefs.api.app:app --host 0.0.0.0 --port 8000
```

### Verify the service

```bash
curl http://localhost:8000/health
```

> **Interactive API docs** are available at **`http://localhost:8000/docs`**: open it in your browser to explore and try all endpoints interactively.

## Available endpoints

For the full API reference with all parameters and examples, see the [pipeline reference guide](https://docs.bigdata.com/use-cases/bigdata-briefs-pipeline).

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

#### `POST /api/v1/batch/run`

Same as `run-parallel` but processes entities **sequentially** one after the other. Useful for controlled, lower-concurrency runs.

```bash
curl -X POST http://localhost:8000/api/v1/batch/run \
  -H "Content-Type: application/json" \
  -d '{
    "entity_ids": ["0157B1", "D64C6D"],
    "force_window_start": "2026-04-22T00:00:00",
    "force_window_end": "2026-04-22T23:59:59"
  }'
```

### Monitor a batch

#### `GET /api/v1/batch/parallel/{batch_id}/status`

Returns the real-time status of a batch submitted via `run-parallel`.

```bash
curl http://localhost:8000/api/v1/batch/parallel/3f8a1c2d-.../status
```

### Daily tracking

For recurring monitoring of a portfolio, two patterns are available: **update** and **scan**.

#### `POST /api/v1/batch/run-parallel` with `window_mode: update`

`update` is the standard pattern for keeping coverage current. Each run covers at most the 24 hours preceding the run time — extended to 72 hours on Mondays (UTC) to bridge the weekend gap. If a previous run exists and its end timestamp falls within that lookback window, the new run starts from there.

This makes `update` self-initializing: the first call for a new entity produces the first brief; subsequent daily calls continue seamlessly from where the last run ended.

```bash
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "window_mode": "update",
    "categories": ["news"]
  }'
```

#### `POST /api/v1/scan`

Use `scan` when you need to build or backfill a historical record for a portfolio. It takes an explicit date range, splits it into individual calendar-day windows, and processes them sequentially.

```bash
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "start_date": "2026-04-01T00:00:00",
    "end_date": "2026-04-30T23:59:59",
    "source_categories": ["news"]
  }'
```

Poll per-entity, per-day progress:

```bash
curl "http://localhost:8000/api/v1/scan/status?entity_ids=D8442A,0157B1&start_date=2026-04-01&end_date=2026-04-30"
```

**Recommended workflow for a new portfolio:**

```bash
# Step 1: build history (optional)
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "start_date": "2026-04-01T00:00:00",
    "end_date": "2026-04-30T23:59:59",
    "source_categories": ["news"]
  }'

# Step 2: daily update (run once per day from here on)
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{
    "universe": "dow_30",
    "window_mode": "update",
    "categories": ["news"]
  }'
```

### Retrieve results

#### `POST /api/v1/batch/bullets`

Returns the published bullet points for one or more entities, grouped by run. Each bullet includes the final text, source citations (headline, chunk text), and novelty metadata (`search_action`, `not_fully_novel`). Pass an empty `entity_ids` list to retrieve all entities in the database.

```bash
curl -X POST http://localhost:8000/api/v1/batch/bullets \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1", "D64C6D"]}'
```

#### `POST /api/v1/batch/bullets/detail`

Returns every bullet considered by the pipeline — both published and discarded — for one or more entities. For discarded bullets, includes the stage that eliminated them and the specific reason for that decision:

- `relevance_score`: scored too low on financial materiality
- `grounding`: text not verifiable against cited sources
- `novelty_embedding`: already reported in a previous run
- `novelty_search`: per-claim verdicts with the evidence chunks that already covered the information

Accepts optional `from_date` and `to_date` filters (ISO 8601).

```bash
curl -X POST http://localhost:8000/api/v1/batch/bullets/detail \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1"]}'
```

### HTML report

#### `GET /api/v1/report/html`

Generates a self-contained HTML page. Published bullets are shown in **green** (fully novel) or **amber** (partially novel, rewritten to surface the new element). Discarded bullets are grouped under a collapsible section showing the reason, stage, and (for novelty search discards) the prior evidence that already covered each claim.

```
http://localhost:8000/api/v1/report/html?entity_id=0157B1
```

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

## Window modes

Every run covers a time window `[start, end)`. You can specify it explicitly with `force_window_start` / `force_window_end`, or let the pipeline compute it automatically via `window_mode`.

### `update` (recommended for daily monitoring)

Covers at most the 24 hours preceding the run time, extended to 72 hours on Mondays to bridge the weekend gap. If a previous run exists and its end timestamp falls within that lookback window, starts from there instead.

### `daily` (default)

Covers `[UTC midnight of today → now]`.

- If the pipeline already ran **today**, it resumes from exactly where that run ended.
- If the last run was **yesterday or earlier**, it always resets to midnight of today.

### `continuous`

Covers `[end of last run → now]`.

- If the last run was yesterday at 18:00, today's run covers from 18:00 yesterday to now: no gap, no reset.
- If no previous run exists, falls back to `[UTC midnight of today → now]`.

| | `update` | `daily` | `continuous` |
|---|---|---|---|
| No previous run | `[now − 24h → now]` | `[today midnight → now]` | `[today midnight → now]` |
| Last run was today at 09:00 | `[09:00 → now]` | `[09:00 → now]` | `[09:00 → now]` |
| Last run was yesterday at 18:00 | `[yesterday 18:00 → now]` | `[today midnight → now]` | `[yesterday 18:00 → now]` |
| Last run was 3 days ago | `[now − 24h → now]` | `[today midnight → now]` | `[3 days ago end → now]` |

Use `update` for standard day-by-day monitoring. Use `daily` when you want a hard reset to midnight of today regardless of prior runs. Use `continuous` when you need a gap-free timeline across runs regardless of when they last triggered.

> **Overlap protection**: if the requested window overlaps any already-completed run for the same entity, that entity's run is rejected immediately and marked as `failed`. No API or LLM calls are made.

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

## Configuration reference

| Environment variable | Description | Default |
|---|---|---|
| `BIGDATA_API_KEY` | Bigdata.com API key **(required)** | — |
| `OPENAI_API_KEY` | OpenAI API key **(required)** | — |
| `MAX_CONCURRENT_ENTITIES` | Max entities running in parallel | `10` |
| `DB_STRING` | SQLite connection string | `sqlite:///briefs.db` |
| `LLM_TIMEOUT_SECONDS` | LLM call timeout | `60` |
| `NOVELTY_LOOKBACK_DAYS` | Days of history used for novelty checks | `14` |

See `.env.example` for the full list with descriptions.

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

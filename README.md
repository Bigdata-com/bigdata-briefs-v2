# Bigdata Briefs v2.0

An AI-powered pipeline that generates financially relevant, novelty-filtered brief reports for a universe of companies. For each entity and reporting window, the service fetches news, extracts key bullet points, filters out previously reported content via embedding-based and search-based novelty checks, and exposes the results through a REST API.

## Architecture overview

![Pipeline diagram](assets/bigdata_briefs_overview.png)

The pipeline processes each entity through five sequential phases:

1. **Search** — exploratory + concept-driven search via Bigdata.com API
2. **Bullet Generation** — LLM extracts material bullet points per theme
3. **Novelty Check via Embedding** — discards bullets already covered in prior runs
4. **Novelty Check via Search** — claim-level verification against recent news evidence
5. **Post-processing** — redundancy removal, thematic consolidation, report assembly *(in development)*

---

## Prerequisites

- A **Bigdata.com API key**
- An **OpenAI API key**
- **Docker** (option A) or **uv** (option B)

---

## Quickstart

### Option A — Docker

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

### Option B — uv (no Docker)

```bash
# Install uv if needed: https://docs.astral.sh/uv/getting-started/installation/

# Install dependencies
uv sync

# Copy and edit the environment file
cp .env.example .env

# Run
uv run uvicorn bigdata_briefs.api.app:app --host 0.0.0.0 --port 8000
```

### Verify the service

```bash
curl http://localhost:8000/health
```

> **Interactive API docs** are available at **`http://localhost:8000/docs`** — open it in your browser to explore and try all endpoints interactively.

---

## Available endpoints

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

**Response:**
```json
{
  "batch_id": "3f8a1c2d-...",
  "total": 10,
  "submitted_at": "2026-04-22T09:00:00"
}
```

> `entity_ids` and `universe` are mutually exclusive. Available universes: `dow_30`, `eurostoxx_50`, `top_us_100`, `top_us_500`, `top_eu_100`, `top_eu_500`.  
> Omit `force_window_start` / `force_window_end` to use the automatic incremental window.  
> Re-running a window that overlaps an already-completed run for the same entity is blocked automatically.

---

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

---

### Monitor a batch

#### `GET /api/v1/batch/parallel/{batch_id}/status`

Returns the real-time status of a batch submitted via `run-parallel`: how many entities have succeeded, failed, are still running, or have not started yet.

```bash
curl http://localhost:8000/api/v1/batch/parallel/3f8a1c2d-.../status
```

**Response:**
```json
{
  "batch_id": "3f8a1c2d-...",
  "total": 10,
  "succeeded": 8,
  "failed": 1,
  "running": 1,
  "not_started": 0,
  "runs": [
    {
      "entity_id": "0157B1",
      "run_id": "...",
      "status": "succeeded",
      "started_at": "2026-04-22T09:00:03",
      "completed_at": "2026-04-22T09:04:51"
    }
  ]
}
```

---

### Retrieve results

#### `POST /api/v1/batch/bullets`

Returns the published bullet points for one or more entities, grouped by run. Pass an empty body to retrieve all entities in the database.

```bash
curl -X POST http://localhost:8000/api/v1/batch/bullets \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1", "D64C6D"]}'

# All entities
curl -X POST http://localhost:8000/api/v1/batch/bullets \
  -H "Content-Type: application/json" \
  -d '{}'
```

Each bullet includes the final text, source citations (headline, chunk text), and novelty metadata (`search_action`, `not_fully_novel`).

---

#### `POST /api/v1/batch/bullets/detail`

Returns **full pipeline detail** for every bullet — both published and discarded — for one or more entities. Pass an empty body to retrieve all entities.

For each bullet you get:
- **Published bullets**: relevance score and reasoning that justified publishing
- **Discarded bullets**: the stage that eliminated them and the reason:
  - `relevance_score` — scored too low on financial materiality
  - `grounding` — text not verifiable against cited sources
  - `novelty_embedding` — already reported in a previous run
  - `novelty_search` — per-claim verdicts with the evidence chunks that already covered the information

```bash
curl -X POST http://localhost:8000/api/v1/batch/bullets/detail \
  -H "Content-Type: application/json" \
  -d '{"entity_ids": ["0157B1"]}'
```

---

### HTML report

#### `GET /api/v1/report/html`

Generates a self-contained HTML page that can be opened directly in the browser. Published bullets are shown in **green** (fully novel) or **amber** (partially novel, rewritten to surface the new element). Discarded bullets are grouped under a collapsible **Discard** section showing the reason, stage, and — for novelty search discards — the prior evidence that already covered each claim.

```
# All entities — open directly in browser
http://localhost:8000/api/v1/report/html

# Single entity
http://localhost:8000/api/v1/report/html?entity_id=0157B1

# With API key
http://localhost:8000/api/v1/report/html?entity_id=0157B1&api_key=<your-secret-key>
```

Download via curl:
```bash
curl "http://localhost:8000/api/v1/report/html?entity_id=0157B1" -o report.html
```

---

### Administration

#### `POST /api/v1/admin/reset-db`

**Drops and recreates all database tables.** Use with caution — all run history, embeddings, and saved bullets are permanently deleted.

```bash
curl -X POST http://localhost:8000/api/v1/admin/reset-db
```

Useful when starting a fresh evaluation or clearing test data before a new run.

---

#### `POST /api/v1/admin/clear-stale-runs`

Resets rows that are stuck in `running` status (e.g. after a service crash). Rows older than the configured threshold are marked as `failed` so they no longer block re-runs for the same entity.

```bash
curl -X POST http://localhost:8000/api/v1/admin/clear-stale-runs
```

---

## Understanding run windows

Every run covers a time window `[start, end)`. You can either specify it explicitly with `force_window_start` / `force_window_end`, or let the pipeline compute it automatically via `window_mode`.

**Explicit window** — use `force_window_start` and `force_window_end` to target a specific period. One day at a time is recommended: a single-day window gives the model a focused, bounded set of news to analyze, producing sharper bullets and more reliable novelty comparisons. Wider windows are possible but for entities with a high volume of news they can generate briefs where temporal references are ambiguous or inconsistent.

**Automatic window** — omit the dates and let `window_mode` decide:

### `daily` (default)

Covers `[UTC midnight of today → now]`.

- If the pipeline already ran **today**, it resumes from exactly where that run ended.
- If the last run was **yesterday or earlier**, it always resets to midnight of today — prior days never influence today's window start.

### `continuous`

Covers `[end of last run → now]`.

- If the last run was yesterday at 18:00, today's run covers from 18:00 yesterday to now — no gap, no reset.
- If no previous run exists, falls back to `[UTC midnight of today → now]`, same as `daily`.

| | `daily` | `continuous` |
|---|---|---|
| No previous run | `[today midnight → now]` | `[today midnight → now]` |
| Last run was today at 09:00 | `[09:00 → now]` | `[09:00 → now]` |
| Last run was yesterday at 18:00 | `[today midnight → now]` | `[yesterday 18:00 → now]` |
| Last run was 3 days ago | `[today midnight → now]` | `[3 days ago end → now]` |

Use `daily` for standard day-by-day monitoring. Use `continuous` when you need to guarantee no gaps across runs regardless of when they were last triggered.

```bash
curl -X POST http://localhost:8000/api/v1/batch/run-parallel \
  -H "Content-Type: application/json" \
  -d '{"universe": "dow_30", "window_mode": "continuous"}'
```

> **Overlap protection** — if the requested window (forced or automatic) overlaps any already-completed run for the same entity, that entity's run is rejected immediately with an error and marked as `failed` in the batch status. No API calls or LLM calls are made. Use a non-overlapping date range or reset the entity via `POST /api/v1/admin/reset-db` to clear its history.

---

## Pre-defined universes

Six entity universes are bundled with the service:

| Universe | Entities | Description |
|---|---|---|
| `dow_30` | 30 | Dow Jones Industrial Average components |
| `eurostoxx_50` | 50 | Euro Stoxx 50 components |
| `top_us_100` | 100 | Top 100 US companies by market cap |
| `top_us_500` | 500 | Top 500 US companies by market cap |
| `top_eu_100` | 100 | Top 100 European companies by market cap |
| `top_eu_500` | 500 | Top 500 European companies by market cap |

Pass `"universe": "top_us_100"` (or any name above) to `run-parallel` or `run` instead of an explicit `entity_ids` list.

---

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

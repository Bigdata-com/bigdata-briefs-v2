# Stateless latency benchmarks

Measure average wall-clock latency for **stateless 24h briefs** across three company-size cohorts (10 entities each):

| Cohort | Definition |
|--------|------------|
| **large** | `top_us_10` mega-caps (highest news volume) |
| **mid** | US `top_us_500` names outside `top_us_100`, sampled around median news-volume rank |
| **small** | Lowest-ranked US `top_us_500` names with enough daily volume to run reliably |

Entity lists are frozen in [`cohorts.json`](cohorts.json). Regenerate from packaged universe data with `--regenerate-cohorts`.

## Prerequisites

- `BIGDATA_API_KEY` and `OPENAI_API_KEY` in `.env`
- Python **3.11–3.13** recommended (avoid 3.14 until SQLModel/Pydantic support is verified)

## Quick start

```bash
# Inspect cohorts without API calls
uv run benchmark-stateless-latency --dry-run

# Run all 30 entities sequentially (clean per-entity latency)
uv run benchmark-stateless-latency --output benchmarks/results/latest.json

# Run one cohort only
uv run benchmark-stateless-latency --cohort large

# Match production fan-out concurrency
uv run benchmark-stateless-latency --max-workers 10 --output benchmarks/results/parallel.json
```

## Output

Stdout prints per-cohort **mean, median, p95, min, max** plus per-entity timings. Optional `--output` writes full JSON including bullet counts and errors.

## Notes

- Default concurrency is **1** so each entity's latency is not skewed by parallel runs.
- Use a fixed `--window-end` (ISO-8601 UTC) for reproducible comparisons across runs.
- First run on a fresh machine may be slow while `uv` builds the virtualenv; run `uv sync` once before benchmarking.

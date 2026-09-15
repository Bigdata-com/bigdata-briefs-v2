"""Run stateless 24h brief latency benchmarks across company-size cohorts."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Semaphore
from time import perf_counter
from typing import Any

import httpx
from dotenv import load_dotenv

from bigdata_briefs.benchmarks.cohorts import (
    BenchmarkCohorts,
    BenchmarkEntity,
    CohortName,
    build_cohorts_from_costs,
    load_frozen_cohorts,
    save_frozen_cohorts,
)
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.stateless_runner import run_entity_stateless
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import UNSET, settings

_BIGDATA_MAX_QPM = 450
_BIGDATA_RATE_REFRESH_SECONDS = 5
_BIGDATA_RATE_RETRY_SECONDS = 1.0


@dataclass(frozen=True)
class EntityLatencyResult:
    cohort: CohortName
    entity_id: str
    entity_name: str
    rank: int
    volume_chunks: int
    latency_seconds: float
    bullets_saved: int
    bullets_discarded: int
    error: str | None = None


@dataclass(frozen=True)
class CohortLatencySummary:
    cohort: CohortName
    count: int
    successes: int
    failures: int
    mean_seconds: float | None
    median_seconds: float | None
    min_seconds: float | None
    max_seconds: float | None
    p95_seconds: float | None
    stdev_seconds: float | None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        raise ValueError("cannot compute percentile of empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_cohort(
    cohort: CohortName,
    results: list[EntityLatencyResult],
) -> CohortLatencySummary:
    cohort_results = [result for result in results if result.cohort == cohort]
    successes = [result for result in cohort_results if result.error is None]
    latencies = [result.latency_seconds for result in successes]
    return CohortLatencySummary(
        cohort=cohort,
        count=len(cohort_results),
        successes=len(successes),
        failures=len(cohort_results) - len(successes),
        mean_seconds=statistics.fmean(latencies) if latencies else None,
        median_seconds=statistics.median(latencies) if latencies else None,
        min_seconds=min(latencies) if latencies else None,
        max_seconds=max(latencies) if latencies else None,
        p95_seconds=_percentile(latencies, 0.95) if latencies else None,
        stdev_seconds=statistics.pstdev(latencies) if len(latencies) > 1 else None,
    )


def _ensure_api_keys() -> None:
    missing: list[str] = []
    if settings.BIGDATA_API_KEY == UNSET:
        missing.append("BIGDATA_API_KEY")
    if settings.OPENAI_API_KEY == UNSET:
        missing.append("OPENAI_API_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def _build_runtime(
    *,
    max_workers: int,
) -> tuple[
    RequestsPerMinuteController,
    Semaphore,
    httpx.Client,
    ThreadPoolExecutor,
]:
    rate_limiter = RequestsPerMinuteController(
        max_requests_per_min=_BIGDATA_MAX_QPM,
        rate_limit_refresh_frequency=_BIGDATA_RATE_REFRESH_SECONDS,
        seconds_before_retry=_BIGDATA_RATE_RETRY_SECONDS,
    )
    connection_sem = Semaphore(settings.API_SIMULTANEOUS_REQUESTS)
    http_client = httpx.Client(
        base_url=settings.API_BASE_URL,
        headers={
            "X-API-KEY": str(settings.BIGDATA_API_KEY),
            "Content-Type": "application/json",
        },
        timeout=settings.API_TIMEOUT_SECONDS,
    )
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bench-entity")
    return rate_limiter, connection_sem, http_client, executor


def _run_single_entity(
    *,
    cohort: CohortName,
    entity: BenchmarkEntity,
    window_start: datetime,
    window_end: datetime,
    pipeline_config: dict[str, Any],
    rate_limiter: RequestsPerMinuteController,
    connection_sem: Semaphore,
    http_client: httpx.Client,
) -> EntityLatencyResult:
    started = perf_counter()
    try:
        report = run_entity_stateless(
            entity_id=entity.entity_id,
            window_start=window_start,
            window_end=window_end,
            pipeline_config=pipeline_config,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
        )
        latency = perf_counter() - started
        return EntityLatencyResult(
            cohort=cohort,
            entity_id=entity.entity_id,
            entity_name=entity.name,
            rank=entity.rank,
            volume_chunks=entity.volume_chunks,
            latency_seconds=latency,
            bullets_saved=int(report.get("bullets_saved") or 0),
            bullets_discarded=int(report.get("bullets_discarded") or 0),
        )
    except Exception as exc:  # noqa: BLE001 — benchmark should capture per-entity failures
        latency = perf_counter() - started
        return EntityLatencyResult(
            cohort=cohort,
            entity_id=entity.entity_id,
            entity_name=entity.name,
            rank=entity.rank,
            volume_chunks=entity.volume_chunks,
            latency_seconds=latency,
            bullets_saved=0,
            bullets_discarded=0,
            error=str(exc),
        )


def run_benchmark(
    *,
    cohorts: BenchmarkCohorts,
    selected_cohorts: tuple[CohortName, ...],
    window_start: datetime,
    window_end: datetime,
    max_workers: int = 1,
) -> list[EntityLatencyResult]:
    _ensure_api_keys()
    pipeline_config = load_pipeline_config_dict(resolve_config_path(None))
    rate_limiter, connection_sem, http_client, executor = _build_runtime(
        max_workers=max_workers,
    )
    results: list[EntityLatencyResult] = []

    try:
        for cohort_name in selected_cohorts:
            entities = cohorts.by_name(cohort_name)
            futures = {
                executor.submit(
                    _run_single_entity,
                    cohort=cohort_name,
                    entity=entity,
                    window_start=window_start,
                    window_end=window_end,
                    pipeline_config=pipeline_config,
                    rate_limiter=rate_limiter,
                    connection_sem=connection_sem,
                    http_client=http_client,
                ): entity
                for entity in entities
            }
            for future in as_completed(futures):
                results.append(future.result())
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        http_client.close()

    return results


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}s"


def _print_summary(
    *,
    results: list[EntityLatencyResult],
    window_start: datetime,
    window_end: datetime,
    max_workers: int,
) -> None:
    print("Stateless 24h brief latency benchmark")
    print(f"Window: {window_start.isoformat()} -> {window_end.isoformat()}")
    print(f"Concurrency: {max_workers}")
    print()

    for cohort_name in ("large", "mid", "small"):
        summary = summarize_cohort(cohort_name, results)
        print(f"[{cohort_name}] n={summary.count} ok={summary.successes} err={summary.failures}")
        print(
            "  mean={mean}  median={median}  p95={p95}  min={min}  max={max}".format(
                mean=_format_seconds(summary.mean_seconds),
                median=_format_seconds(summary.median_seconds),
                p95=_format_seconds(summary.p95_seconds),
                min=_format_seconds(summary.min_seconds),
                max=_format_seconds(summary.max_seconds),
            )
        )
        for result in sorted(
            (item for item in results if item.cohort == cohort_name),
            key=lambda item: item.rank,
        ):
            status = "ok" if result.error is None else f"error: {result.error}"
            print(
                f"  - {result.entity_name} ({result.entity_id}) "
                f"rank={result.rank} latency={result.latency_seconds:.1f}s "
                f"bullets={result.bullets_saved} [{status}]"
            )
        print()


def _build_report(
    *,
    results: list[EntityLatencyResult],
    window_start: datetime,
    window_end: datetime,
    max_workers: int,
    cohort_source: str,
) -> dict[str, object]:
    summaries = {
        cohort_name: asdict(summarize_cohort(cohort_name, results))
        for cohort_name in ("large", "mid", "small")
    }
    return {
        "benchmark": "stateless_24h_latency",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "max_workers": max_workers,
        "cohort_source": cohort_source,
        "summaries": summaries,
        "results": [asdict(result) for result in results],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure average latency of stateless 24h briefs for large, mid, and small "
            "US companies (10 each by default)."
        )
    )
    parser.add_argument(
        "--cohort",
        choices=["large", "mid", "small", "all"],
        default="all",
        help="Which cohort to benchmark (default: all).",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="Rolling lookback window in hours (default: 24).",
    )
    parser.add_argument(
        "--window-end",
        type=str,
        default="",
        help="ISO-8601 UTC window end (default: now).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Parallel entity runs (default: 1 for clean per-entity latency).",
    )
    parser.add_argument(
        "--cohorts-file",
        type=Path,
        default=None,
        help="Frozen cohort JSON (default: benchmarks/cohorts.json).",
    )
    parser.add_argument(
        "--regenerate-cohorts",
        action="store_true",
        help="Rebuild benchmarks/cohorts.json from universe cost data and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected cohorts without calling external APIs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write JSON results.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)

    if args.regenerate_cohorts:
        cohorts = build_cohorts_from_costs()
        save_frozen_cohorts(cohorts)
        print(f"Wrote cohort definitions to benchmarks/cohorts.json")
        return 0

    cohort_source = "frozen"
    if args.cohorts_file is not None:
        cohorts = load_frozen_cohorts(args.cohorts_file)
        cohort_source = str(args.cohorts_file)
    else:
        try:
            cohorts = load_frozen_cohorts()
        except FileNotFoundError:
            cohorts = build_cohorts_from_costs()
            save_frozen_cohorts(cohorts)
            cohort_source = "generated"

    selected: tuple[CohortName, ...]
    if args.cohort == "all":
        selected = ("large", "mid", "small")
    else:
        selected = (args.cohort,)  # type: ignore[assignment]

    if args.window_end:
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
    else:
        window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=args.window_hours)

    if args.dry_run:
        print("Dry run — selected cohorts:")
        for cohort_name in selected:
            print(f"[{cohort_name}]")
            for entity in cohorts.by_name(cohort_name):
                print(
                    f"  {entity.entity_id}  {entity.name}  "
                    f"rank={entity.rank}  volume_chunks={entity.volume_chunks}"
                )
        print(f"Window: {window_start.isoformat()} -> {window_end.isoformat()}")
        return 0

    results = run_benchmark(
        cohorts=cohorts,
        selected_cohorts=selected,
        window_start=window_start,
        window_end=window_end,
        max_workers=max(1, args.max_workers),
    )
    _print_summary(
        results=results,
        window_start=window_start,
        window_end=window_end,
        max_workers=max(1, args.max_workers),
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report = _build_report(
            results=results,
            window_start=window_start,
            window_end=window_end,
            max_workers=max(1, args.max_workers),
            cohort_source=cohort_source,
        )
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote JSON report to {args.output}")

    failures = sum(1 for result in results if result.error is not None)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for stateless latency benchmark cohort selection and summaries."""

from __future__ import annotations

from bigdata_briefs.benchmarks.cohorts import (
    COHORT_SIZE,
    build_cohorts_from_costs,
    cohorts_from_dict,
    cohorts_to_dict,
)
from bigdata_briefs.benchmarks.stateless_latency import (
    EntityLatencyResult,
    summarize_cohort,
)


def test_build_cohorts_returns_ten_entities_per_bucket() -> None:
    cohorts = build_cohorts_from_costs()

    assert len(cohorts.large) == COHORT_SIZE
    assert len(cohorts.mid) == COHORT_SIZE
    assert len(cohorts.small) == COHORT_SIZE
    assert len(cohorts.all_entities()) == COHORT_SIZE * 3


def test_large_cohort_uses_top_us_10_members() -> None:
    cohorts = build_cohorts_from_costs()
    large_ids = {entity.entity_id for entity in cohorts.large}

    assert "E09E2B" in large_ids  # NVIDIA
    assert "D8442A" in large_ids  # Apple
    assert all(entity.rank <= 50 for entity in cohorts.large)


def test_mid_and_small_cohorts_are_lower_volume_than_large() -> None:
    cohorts = build_cohorts_from_costs()
    large_max_rank = max(entity.rank for entity in cohorts.large)
    mid_median_rank = sorted(entity.rank for entity in cohorts.mid)[len(cohorts.mid) // 2]
    small_min_rank = min(entity.rank for entity in cohorts.small)

    assert large_max_rank < mid_median_rank < small_min_rank


def test_cohorts_round_trip_json() -> None:
    original = build_cohorts_from_costs()
    restored = cohorts_from_dict(cohorts_to_dict(original))

    assert restored.large == original.large
    assert restored.mid == original.mid
    assert restored.small == original.small


def test_summarize_cohort_computes_mean_and_p95() -> None:
    results = [
        EntityLatencyResult(
            cohort="large",
            entity_id="A",
            entity_name="A Inc.",
            rank=1,
            volume_chunks=100_000,
            latency_seconds=100.0,
            bullets_saved=3,
            bullets_discarded=1,
        ),
        EntityLatencyResult(
            cohort="large",
            entity_id="B",
            entity_name="B Inc.",
            rank=2,
            volume_chunks=90_000,
            latency_seconds=120.0,
            bullets_saved=2,
            bullets_discarded=0,
        ),
        EntityLatencyResult(
            cohort="large",
            entity_id="C",
            entity_name="C Inc.",
            rank=3,
            volume_chunks=80_000,
            latency_seconds=80.0,
            bullets_saved=1,
            bullets_discarded=2,
            error="timeout",
        ),
    ]

    summary = summarize_cohort("large", results)

    assert summary.count == 3
    assert summary.successes == 2
    assert summary.failures == 1
    assert summary.mean_seconds == 110.0
    assert summary.median_seconds == 110.0
    assert summary.min_seconds == 100.0
    assert summary.max_seconds == 120.0
    assert summary.p95_seconds == 119.0

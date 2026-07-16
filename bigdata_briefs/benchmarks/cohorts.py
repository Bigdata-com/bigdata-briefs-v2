"""Entity cohort selection for stateless latency benchmarks.

Company size buckets are defined by news-volume rank within the US ``top_us_500``
universe (from ``universe_entity_costs.csv``). Higher rank (lower number) means
more daily news volume and typically higher pipeline latency.

- **large**: ``top_us_10`` members (mega-cap, highest news volume)
- **mid**: US ``top_us_500`` names outside ``top_us_100``, sampled around the median rank
- **small**: lowest-ranked US ``top_us_500`` names with enough volume to run reliably
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

CohortName = Literal["large", "mid", "small"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_COSTS_CSV = PROJECT_ROOT / "bigdata_briefs" / "data" / "universe_entity_costs.csv"
DEFAULT_UNIVERSES_DIR = PROJECT_ROOT / "bigdata_briefs" / "data" / "universes"
DEFAULT_FROZEN_COHORTS_JSON = PROJECT_ROOT / "benchmarks" / "cohorts.json"

COHORT_SIZE = 10
MID_MIN_VOLUME_CHUNKS = 5_000
SMALL_MIN_VOLUME_CHUNKS = 500


@dataclass(frozen=True)
class BenchmarkEntity:
    entity_id: str
    name: str
    rank: int
    volume_chunks: int
    universes: str


@dataclass(frozen=True)
class BenchmarkCohorts:
    large: tuple[BenchmarkEntity, ...]
    mid: tuple[BenchmarkEntity, ...]
    small: tuple[BenchmarkEntity, ...]

    def by_name(self, cohort: CohortName) -> tuple[BenchmarkEntity, ...]:
        if cohort == "large":
            return self.large
        if cohort == "mid":
            return self.mid
        if cohort == "small":
            return self.small
        raise ValueError(f"unknown cohort: {cohort}")

    def all_entities(self) -> tuple[BenchmarkEntity, ...]:
        return self.large + self.mid + self.small


def _load_universe_ids(universes_dir: Path, stem: str) -> set[str]:
    path = universes_dir / f"{stem}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["id"].strip() for row in csv.DictReader(handle) if row.get("id")}


def _is_curated_us_row(row: dict[str, str]) -> bool:
    if row.get("region", "").strip() != "US":
        return False
    universes = (row.get("universes") or "").strip()
    return bool(universes) and not universes.startswith("index_")


def _parse_entity_row(row: dict[str, str]) -> BenchmarkEntity | None:
    entity_id = (row.get("entity_id") or "").strip()
    name = (row.get("name") or "").strip()
    universes = (row.get("universes") or "").strip()
    if not entity_id or not name:
        return None
    try:
        rank = int(row.get("rank") or "")
        volume_chunks = int(row.get("volume_chunks") or "0")
    except ValueError:
        return None
    return BenchmarkEntity(
        entity_id=entity_id,
        name=name,
        rank=rank,
        volume_chunks=volume_chunks,
        universes=universes,
    )


def build_cohorts_from_costs(
    *,
    costs_csv: Path = DEFAULT_COSTS_CSV,
    universes_dir: Path = DEFAULT_UNIVERSES_DIR,
    cohort_size: int = COHORT_SIZE,
) -> BenchmarkCohorts:
    """Derive large / mid / small cohorts from packaged universe cost data."""
    top_us_10 = _load_universe_ids(universes_dir, "top_us_10")
    top_us_100 = _load_universe_ids(universes_dir, "top_us_100")
    top_us_500 = _load_universe_ids(universes_dir, "top_us_500")

    by_id: dict[str, BenchmarkEntity] = {}
    with costs_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not _is_curated_us_row(row):
                continue
            entity = _parse_entity_row(row)
            if entity is None or entity.entity_id not in top_us_500:
                continue
            by_id[entity.entity_id] = entity

    ranked = sorted(by_id.values(), key=lambda entity: entity.rank)

    large_pool = [entity for entity in ranked if entity.entity_id in top_us_10]
    if len(large_pool) < cohort_size:
        large_pool = ranked[:cohort_size]

    mid_pool = [
        entity
        for entity in ranked
        if entity.entity_id not in top_us_100 and entity.volume_chunks >= MID_MIN_VOLUME_CHUNKS
    ]
    mid_start = max(0, (len(mid_pool) // 2) - (cohort_size // 2))
    mid = mid_pool[mid_start : mid_start + cohort_size]

    small_pool = [
        entity
        for entity in ranked
        if entity.entity_id not in top_us_100 and entity.volume_chunks >= SMALL_MIN_VOLUME_CHUNKS
    ]
    small = small_pool[-cohort_size:]

    if len(large_pool) < cohort_size:
        raise ValueError(f"large cohort needs {cohort_size} entities, found {len(large_pool)}")
    if len(mid) < cohort_size:
        raise ValueError(f"mid cohort needs {cohort_size} entities, found {len(mid)}")
    if len(small) < cohort_size:
        raise ValueError(f"small cohort needs {cohort_size} entities, found {len(small)}")

    return BenchmarkCohorts(
        large=tuple(large_pool[:cohort_size]),
        mid=tuple(mid),
        small=tuple(small),
    )


def cohorts_to_dict(cohorts: BenchmarkCohorts) -> dict[str, list[dict[str, object]]]:
    return {
        "large": [asdict(entity) for entity in cohorts.large],
        "mid": [asdict(entity) for entity in cohorts.mid],
        "small": [asdict(entity) for entity in cohorts.small],
    }


def cohorts_from_dict(payload: dict[str, object]) -> BenchmarkCohorts:
    def _parse_bucket(bucket_name: str) -> tuple[BenchmarkEntity, ...]:
        raw = payload.get(bucket_name)
        if not isinstance(raw, list):
            raise ValueError(f"cohorts payload missing list for {bucket_name!r}")
        entities: list[BenchmarkEntity] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"invalid entity record in {bucket_name!r}")
            entities.append(
                BenchmarkEntity(
                    entity_id=str(item["entity_id"]),
                    name=str(item["name"]),
                    rank=int(item["rank"]),
                    volume_chunks=int(item["volume_chunks"]),
                    universes=str(item.get("universes", "")),
                )
            )
        return tuple(entities)

    return BenchmarkCohorts(
        large=_parse_bucket("large"),
        mid=_parse_bucket("mid"),
        small=_parse_bucket("small"),
    )


def load_frozen_cohorts(path: Path = DEFAULT_FROZEN_COHORTS_JSON) -> BenchmarkCohorts:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid cohorts JSON at {path}")
    return cohorts_from_dict(payload)


def save_frozen_cohorts(
    cohorts: BenchmarkCohorts,
    path: Path = DEFAULT_FROZEN_COHORTS_JSON,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cohorts_to_dict(cohorts), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

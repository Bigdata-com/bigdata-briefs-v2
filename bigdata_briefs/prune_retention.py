"""CLI: rolling retention prune for the live Briefs SQLite database."""

from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import create_engine

from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.orchestration.retention import default_keep_days, prune_live_database
from bigdata_briefs.settings import settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prune heavy pipeline history older than a rolling retention window "
            "from the live DB (DB_STRING). Does not touch snapshot.db, portfolio, "
            "orchestration KG cache, earnings calendar, or user portfolio."
        )
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=None,
        help="Retention window in days (default: max(30, NOVELTY_LOOKBACK_DAYS))",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete rows (default is dry-run)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run SQLite VACUUM after prune (only with --execute)",
    )
    args = parser.parse_args(argv)

    keep_days = args.keep_days if args.keep_days is not None else default_keep_days()
    dry_run = not args.execute
    vacuum = bool(args.vacuum) and not dry_run

    print(f"prune-retention: DB_STRING={settings.DB_STRING}", file=sys.stderr)
    print(
        f"prune-retention: keep_days={keep_days} dry_run={dry_run} vacuum={vacuum}",
        file=sys.stderr,
    )

    engine = create_engine(settings.DB_STRING, echo=False)
    ensure_orchestration_schema(engine)
    result = prune_live_database(
        engine,
        keep_days=keep_days,
        dry_run=dry_run,
        vacuum=vacuum,
    )
    payload = {
        "cutoff_utc": result.cutoff_utc.isoformat(),
        "keep_days": result.keep_days,
        "dry_run": result.dry_run,
        "vacuum": result.vacuum,
        "deleted": result.deleted,
        "total_deleted": result.total_deleted(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

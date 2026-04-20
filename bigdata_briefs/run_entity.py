"""CLI entrypoint: incremental entity runs via modular ``PipelineRunner``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, create_engine

from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.orchestration.entity_runner import (
    EntityResolutionError,
    EntityRunResult,
    OrchestratorEntityBusyError,
    run_entity_incremental,
)
from bigdata_briefs.orchestration.kg_entities import fetch_kg_entities_by_ids
from bigdata_briefs.orchestration.models import SQLEntityOrchestrationState
from bigdata_briefs.settings import settings


def _parse_iso_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _state_dir(cli: str | None) -> Path:
    if cli:
        return Path(cli).expanduser()
    env = settings.BRIEF_PIPELINE_STATE_DIR.strip()
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".brief_pipeline_state"


def _print_run_entity_paths(*, state_path: Path) -> None:
    """Echo DB URL (SQLite only) and state dir so early exits are easier to diagnose."""
    db = settings.DB_STRING
    if db.startswith("sqlite:"):
        print(f"run-entity: DB_STRING={db}", file=sys.stderr)
    else:
        print("run-entity: DB_STRING=<non-sqlite URL, not echoed>", file=sys.stderr)
    print(f"run-entity: pipeline_state_dir={state_path.resolve()}", file=sys.stderr)


def _build_kg_precache(
    entity_ids: list[str],
    *,
    refresh: bool,
) -> dict[str, dict]:
    eng = create_engine(settings.DB_STRING, echo=False)
    ensure_orchestration_schema(eng)
    to_fetch: list[str] = []
    with Session(eng) as session:
        for eid in entity_ids:
            if refresh:
                to_fetch.append(eid)
                continue
            row = session.get(SQLEntityOrchestrationState, eid)
            if row is None or not row.kg_payload_json:
                to_fetch.append(eid)
    if not to_fetch:
        return {}
    return fetch_kg_entities_by_ids(
        to_fetch,
        api_key=str(settings.BIGDATA_API_KEY),
        base_url=settings.API_BASE_URL,
        timeout_seconds=settings.API_TIMEOUT_SECONDS,
    )


def _print_text_result(res: EntityRunResult) -> None:
    print(f"\n=== entity_id={res.entity_id} ===")
    print(f"window_start={res.report_dates.start.isoformat()}")
    print(f"window_end={res.report_dates.end.isoformat()}")
    print(f"success={res.success} dry_run={res.dry_run}")
    if res.error:
        print(f"error={res.error}")
    print(f"\n--- previous_bullets ({len(res.previous_bullets)}) ---")
    for b in res.previous_bullets:
        print(f"- {b.get('original_text', '')[:120]}...")
    print(f"\n--- new_bullets_novelty_ok ({len(res.new_bullets_novelty_ok)}) ---")
    for b in res.new_bullets_novelty_ok:
        print(f"- {b.get('original_text', '')[:120]}...")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run modular brief pipeline for one or more entities (sequential).",
    )
    parser.add_argument(
        "--entity-id",
        action="append",
        dest="entity_ids",
        required=True,
        help="Entity id (repeat for multiple). A future version may run entities in parallel.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Pipeline YAML/JSON path (overrides BRIEF_PIPELINE_CONFIG).",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="PipelineRunner state + debug logs directory (default: ./.brief_pipeline_state).",
    )
    parser.add_argument(
        "--refresh-entity",
        action="store_true",
        help="Ignore SQLite KG cache and refetch from Knowledge Graph.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only compute window and list previous bullets.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON.")
    parser.add_argument(
        "--force-window-start",
        type=str,
        default=None,
        help="Override report window start (ISO-8601).",
    )
    parser.add_argument(
        "--force-window-end",
        type=str,
        default=None,
        help="Override report window end (ISO-8601).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear stale running lease and allow overlapping recovery.",
    )
    args = parser.parse_args()

    cfg_path = resolve_config_path(args.config)
    pipeline_config = load_pipeline_config_dict(cfg_path)
    state_path = _state_dir(args.state_dir)
    _print_run_entity_paths(state_path=state_path)

    fs = fe = None
    if args.force_window_start and args.force_window_end:
        fs = _parse_iso_dt(args.force_window_start)
        fe = _parse_iso_dt(args.force_window_end)
    elif args.force_window_start or args.force_window_end:
        print("Both --force-window-start and --force-window-end are required together.", file=sys.stderr)
        sys.exit(2)

    precache: dict[str, dict] = {}
    if not args.dry_run:
        try:
            precache = _build_kg_precache(
                args.entity_ids,
                refresh=args.refresh_entity,
            )
        except Exception as e:
            print(f"KG prefetch failed: {e}", file=sys.stderr)
            sys.exit(1)

    results: list[EntityRunResult] = []
    exit_code = 0
    for eid in args.entity_ids:
        try:
            r = run_entity_incremental(
                entity_id=eid,
                pipeline_config=pipeline_config,
                state_dir=state_path,
                refresh_entity=args.refresh_entity,
                dry_run=args.dry_run,
                force_window_start=fs,
                force_window_end=fe,
                force_run=args.force,
                kg_precache=precache or None,
            )
        except OrchestratorEntityBusyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(3)
        except EntityResolutionError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        results.append(r)
        if not r.success and not args.dry_run:
            exit_code = 1

    if args.as_json:
        payload = [
            {
                "entity_id": r.entity_id,
                "success": r.success,
                "dry_run": r.dry_run,
                "error": r.error,
                "report_window_start": r.report_dates.start.isoformat(),
                "report_window_end": r.report_dates.end.isoformat(),
                "previous_bullets": r.previous_bullets,
                "new_bullets_novelty_ok": r.new_bullets_novelty_ok,
                "pipeline_step_results": r.pipeline_step_results,
            }
            for r in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            _print_text_result(r)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

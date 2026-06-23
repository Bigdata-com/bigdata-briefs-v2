"""Backfill editorial narratives for runs that don't have one yet.

Usage:
    uv run python -m bigdata_briefs.backfill_narratives [--dry-run] [--entity-id ID] [--limit N]
    uv run python -m bigdata_briefs.backfill_narratives --delete-all-narratives --yes

For each succeeded run without a narrative in ``sqlrunnarrative``, calls the
same generator the live pipeline uses (``_generate_and_flush_narrative``).

``--delete-all-narratives`` removes existing ``SQLRunNarrative`` rows first
(globally, or only for ``--entity-id`` when set), then backfills like a fresh DB.
Requires ``--yes`` unless ``--dry-run`` (which only prints what would happen).
"""
from __future__ import annotations

import argparse
import sys
from sqlalchemy import create_engine, delete as sa_delete
from sqlmodel import Session, select

from bigdata_briefs.orchestration.entity_runner import (
    _NARRATIVE_PROMPT_FEW,
    _NARRATIVE_PROMPT_MANY,
    _build_citations_text,
    _collect_todays_active_bullets,
    _generate_and_flush_narrative,
)
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLRunNarrative,
)
from bigdata_briefs.models import ReportDates
from bigdata_briefs.settings import settings
from bigdata_briefs.service import BriefPipelineService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="List runs that would be backfilled without calling the LLM.")
    parser.add_argument("--entity-id", help="Restrict backfill to a single entity_id.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of runs to process (0 = no limit).")
    parser.add_argument(
        "--delete-all-narratives",
        action="store_true",
        help="Delete existing SQLRunNarrative rows first (all, or only for --entity-id), then backfill.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive --delete-all-narratives (not needed with --dry-run).",
    )
    args = parser.parse_args()

    if args.delete_all_narratives and not args.yes and not args.dry_run:
        print(
            "Refusing to delete narratives without --yes (or use --dry-run to preview).",
            file=sys.stderr,
        )
        return 1

    eng = create_engine(settings.DB_STRING, echo=False)

    with Session(eng) as session:
        if args.delete_all_narratives:
            del_stmt = sa_delete(SQLRunNarrative)
            if args.entity_id:
                del_stmt = del_stmt.where(SQLRunNarrative.entity_id == args.entity_id)
            count_stmt = select(SQLRunNarrative)
            if args.entity_id:
                count_stmt = count_stmt.where(SQLRunNarrative.entity_id == args.entity_id)
            n_to_delete = len(session.exec(count_stmt).all())
            if args.dry_run:
                print(f"Would delete {n_to_delete} SQLRunNarrative row(s).")
            else:
                session.exec(del_stmt)
                session.commit()
                print(f"Deleted {n_to_delete} SQLRunNarrative row(s).")
                print()

        existing = {n.run_id for n in session.exec(select(SQLRunNarrative)).all()}

        stmt = (
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
            .order_by(SQLEntityPipelineRunLog.process_completed_at_utc)
        )
        if args.entity_id:
            stmt = stmt.where(SQLEntityPipelineRunLog.entity_id == args.entity_id)
        all_runs = session.exec(stmt).all()
        if args.dry_run and args.delete_all_narratives:
            # DB still has rows; simulate post-wipe queue (same as after a real delete).
            missing = list(all_runs)
        else:
            missing = [r for r in all_runs if r.run_id not in existing]

        if args.limit > 0:
            missing = missing[:args.limit]

    if args.dry_run and args.delete_all_narratives:
        print(f"Total succeeded/no_data runs (scope): {len(all_runs)}")
        print(f"Would process after wipe: {len(missing)} run(s) (runs with no active bullets are still skipped).")
        print()
        for r in missing[:50]:
            print(f"  [DRY] {r.entity_id} window_end={r.report_window_end} run_id={str(r.run_id)[:8]}")
        if len(missing) > 50:
            print(f"  ... and {len(missing) - 50} more")
        return 0

    print(f"Total succeeded runs: {len(all_runs)}")
    print(f"Already have narrative: {len(existing)}")
    print(f"Will process: {len(missing)}")
    print()

    if args.dry_run:
        for r in missing:
            print(f"  [DRY] {r.entity_id} window_end={r.report_window_end} run_id={str(r.run_id)[:8]}")
        return 0

    if not missing:
        print("Nothing to do.")
        return 0

    if settings.OPENAI_API_KEY == "<UNSET>":
        print("ERROR: OPENAI_API_KEY is not set — cannot generate narratives.", file=sys.stderr)
        return 1

    # Single LLMClient instance shared across all runs
    brief_service = BriefPipelineService.factory(embedding_storage=None)
    llm_client = brief_service.llm_client

    succeeded = 0
    for i, run in enumerate(missing, 1):
        # Look up entity name from orchestration cache
        with Session(eng) as session:
            orch = session.get(SQLEntityOrchestrationState, run.entity_id)
            # Skip if no active bullets exist for this run
            bullets = session.exec(
                select(SQLBulletRunLog).where(
                    SQLBulletRunLog.run_id == run.run_id,
                    SQLBulletRunLog.is_active == True,  # noqa: E712
                )
            ).all()
        if not bullets:
            print(f"[{i}/{len(missing)}] SKIP {run.entity_id} {run.report_window_end.date() if run.report_window_end else '?'} — no active bullets")
            continue

        entity_name = (orch.kg_name if orch else None) or run.entity_id
        report_dates = ReportDates(
            start=run.report_window_start,
            end=run.report_window_end,
        )

        print(f"[{i}/{len(missing)}] generating for {entity_name} ({run.entity_id}) {run.report_window_end.date() if run.report_window_end else '?'} — {len(bullets)} bullets...", end=" ", flush=True)

        try:
            _generate_and_flush_narrative(
                eng,
                run.run_id,
                run.entity_id,
                entity_name,
                report_dates,
                llm_client,
            )
            # Verify it was written
            with Session(eng) as session:
                n = session.exec(
                    select(SQLRunNarrative).where(SQLRunNarrative.run_id == run.run_id)
                ).first()
            if n:
                print(f"OK ({len(n.narrative_text)} chars)")
                succeeded += 1
            else:
                print("FAILED (no row written)")
        except Exception as e:
            print(f"ERROR: {e}")

    print()
    print(f"Done. Generated {succeeded}/{len(missing)} narratives.")
    return 0 if succeeded == len(missing) else 1


if __name__ == "__main__":
    sys.exit(main())

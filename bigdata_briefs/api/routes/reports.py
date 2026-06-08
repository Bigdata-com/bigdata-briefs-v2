"""
Routes: reports — read-only queries on bullet point results

    GET  /api/v1/reports/{entity_id}/bullets  → latest published bullets for one entity
    POST /api/v1/reports/bullets              → published bullets for N entities (all runs)
    POST /api/v1/reports/bullets/detail       → full pipeline detail (published + discarded) for N entities
    GET  /api/v1/reports/runs/{run_id}/trace  → step-by-step bullet trace for a single run
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc
from sqlmodel import Session, select

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.schemas import (
    BatchBulletsDetailRequest,
    BatchBulletsDetailResponse,
    BatchBulletsRequest,
    BatchBulletsResponse,
    BatchNarrativesRequest,
    BatchNarrativesResponse,
    BulletDetailItem,
    BulletDiscardDetail,
    BulletPassedDetail,
    BulletPointItem,
    BulletTrace,
    CitationDetail,
    ClaimVerdictDetail,
    EmbeddingJudgmentTrace,
    EmbeddingTrace,
    EntityBulletsResult,
    EntityDetailResult,
    EntityNarrativesResult,
    EvidenceDetail,
    GroundingTrace,
    NarrativeItem,
    RelevanceScoringTrace,
    RunBulletsResult,
    RunDetailResult,
    RunTraceResponse,
    SearchTrace,
)
from bigdata_briefs.api.routes.universes import _UNIVERSES, _get_my_portfolio_ids
from bigdata_briefs.novelty.sql_models import SQLGeneratedBulletPoint
from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLRunNarrative,
)

router = APIRouter(tags=["reports"])


# ── Shared helpers ────────────────────────────────────────────────────────────


def _all_entity_ids(engine) -> list[str]:
    with Session(engine) as session:
        rows = session.exec(select(SQLEntityPipelineRunLog.entity_id).distinct()).all()
    return list(rows)


def _source_lookup_from_output_json(
    engine,
    entity_id: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, str]:
    """Return a {CQS:doc_id-chunk_id -> source_name} map from the run's output_json."""
    with Session(engine) as session:
        row = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.report_window_start == window_start)
            .where(SQLEntityPipelineRunLog.report_window_end == window_end)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).first()
    if not row or not row.output_json:
        return {}
    try:
        parsed = json.loads(row.output_json)
        raw_refs: dict = {}
        if isinstance(parsed, list):
            pass
        else:
            raw_refs = parsed.get("source_references") or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    result: dict[str, str] = {}
    for src in raw_refs.values():
        if not isinstance(src, dict):
            continue
        doc_id = src.get("document_id")
        chunk_id = src.get("chunk_id")
        source_name = src.get("source_name", "")
        if doc_id is not None and chunk_id is not None and source_name:
            result[f"CQS:{doc_id}-{chunk_id}"] = source_name
    return result


def _stage_to_category(discard_stage: str | None) -> str | None:
    if discard_stage == "relevance_score":
        return "relevance"
    if discard_stage == "grounding":
        return "grounding"
    if discard_stage in (
        "novelty_embedding", "novelty_embedding_relevance",
        "novelty_search", "novelty_search_relevance",
    ):
        return "novelty"
    return None


def _load_discarded_for_runs(
    run_info: dict[str, tuple[str, datetime, datetime]],
) -> dict[str, dict[str, list[str]]]:
    """
    For each generated run_id, find the matching SQLEntityPipelineRunLog row by
    (entity_id, report_window_start, report_window_end), then read discarded
    bullets from SQLBulletRunLog.

    NOTE: SQLGeneratedBulletPoint.run_id != SQLEntityPipelineRunLog.run_id —
    we match by entity + window, not by UUID.
    """
    if not run_info:
        return {}

    result: dict[str, dict[str, list[str]]] = {}
    engine = get_engine()

    with Session(engine) as session:
        for run_id_str, (entity_id, window_start, window_end) in run_info.items():
            buckets: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}

            log_row = session.exec(
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.report_window_start == window_start)
                .where(SQLEntityPipelineRunLog.report_window_end == window_end)
                .where(SQLEntityPipelineRunLog.status == "succeeded")
                .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).first()

            if not log_row:
                result[run_id_str] = buckets
                continue

            bullet_rows = session.exec(
                select(SQLBulletRunLog)
                .where(SQLBulletRunLog.run_id == log_row.run_id)
                .where(SQLBulletRunLog.is_active == False)  # noqa: E712
            ).all()

            for br in bullet_rows:
                category = _stage_to_category(br.discard_stage)
                if category and br.text:
                    buckets[category].append(br.text)

            result[run_id_str] = buckets

    return result


def _build_entity_result_from_run_log(entity_id: str, engine) -> EntityBulletsResult:
    """Fallback for entities whose bullets were all discarded (nothing in storage)."""
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        entity_name: str | None = orch.kg_name if orch else None

        run_rows = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).all()

    if not run_rows:
        return EntityBulletsResult(entity_id=entity_id, found=False)

    runs: list[RunBulletsResult] = []
    for row in run_rows:
        buckets: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}

        with Session(engine) as session:
            bullet_rows = session.exec(
                select(SQLBulletRunLog)
                .where(SQLBulletRunLog.run_id == row.run_id)
                .where(SQLBulletRunLog.is_active == False)  # noqa: E712
            ).all()

        for br in bullet_rows:
            category = _stage_to_category(br.discard_stage)
            if category and br.text:
                buckets[category].append(br.text)

        bullets_discarded = sum(len(v) for v in buckets.values())
        run_created_at = row.process_completed_at_utc or row.process_started_at_utc
        runs.append(RunBulletsResult(
            run_id=str(row.run_id),
            report_window_start=row.report_window_start,
            report_window_end=row.report_window_end,
            run_created_at=run_created_at,
            bullet_count=0,
            bullets_saved=0,
            bullets_discarded=bullets_discarded,
            bullets=[],
            discarded_by_relevance=buckets["relevance"],
            discarded_by_grounding=buckets["grounding"],
            discarded_by_novelty=buckets["novelty"],
        ))

    return EntityBulletsResult(
        entity_id=entity_id,
        found=True,
        entity_name=entity_name,
        total_runs=len(runs),
        total_bullets=0,
        runs=runs,
    )


def _get_empty_run_results_for_entity(
    engine,
    entity_id: str,
    limit: int,
) -> list[RunBulletsResult]:
    """Return RunBulletsResult entries for recent runs that completed with 0 active bullets.

    These runs exist in SQLEntityPipelineRunLog but have no rows in SQLGeneratedBulletPoint,
    so get_bullets would otherwise skip them and fall back to older stale data.
    """
    with Session(engine) as session:
        recent_logs = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            .limit(limit)
        ).all()

        if not recent_logs:
            return []

        active_run_ids: set[uuid.UUID] = set(session.exec(
            select(SQLBulletRunLog.run_id).distinct()
            .where(SQLBulletRunLog.run_id.in_([r.run_id for r in recent_logs]))
            .where(SQLBulletRunLog.is_active == True)  # noqa: E712
        ).all())

    results: list[RunBulletsResult] = []
    for log_row in recent_logs:
        if log_row.run_id in active_run_ids:
            continue  # has active bullets — already covered by SQLGeneratedBulletPoint

        with Session(engine) as session:
            discarded_rows = session.exec(
                select(SQLBulletRunLog)
                .where(SQLBulletRunLog.run_id == log_row.run_id)
                .where(SQLBulletRunLog.is_active == False)  # noqa: E712
            ).all()

        buckets: dict[str, list[str]] = {"relevance": [], "grounding": [], "novelty": []}
        for br in discarded_rows:
            cat = _stage_to_category(br.discard_stage)
            if cat and br.text:
                buckets[cat].append(br.text)

        run_ts = log_row.process_completed_at_utc or log_row.process_started_at_utc
        bullets_discarded = sum(len(v) for v in buckets.values())
        results.append(RunBulletsResult(
            run_id=str(log_row.run_id),
            report_window_start=log_row.report_window_start,
            report_window_end=log_row.report_window_end,
            run_created_at=run_ts,
            bullet_count=0,
            bullets_saved=0,
            bullets_discarded=bullets_discarded,
            bullets=[],
            discarded_by_relevance=buckets["relevance"],
            discarded_by_grounding=buckets["grounding"],
            discarded_by_novelty=buckets["novelty"],
        ))

    return results


def _resolve_citations(
    citation_ids: list[str],
    source_lookup: dict[str, dict],
) -> list[CitationDetail] | None:
    if not citation_ids:
        return None
    return [
        CitationDetail(
            id=cid,
            headline=(source_lookup.get(cid) or {}).get("headline", ""),
            text=(source_lookup.get(cid) or {}).get("text", ""),
            source_name=(source_lookup.get(cid) or {}).get("source_name", ""),
        )
        for cid in citation_ids
    ] or None


def _build_bullet_detail(
    bp: dict,
    cite_map: dict[str, list[CitationDetail]] | None = None,
    source_lookup: dict[str, dict] | None = None,
) -> BulletDetailItem:
    is_active = bp.get("is_active", True)
    generation = bp.get("generation") or {}
    original_text = generation.get("original_text") or bp.get("text", "")
    final_text = bp.get("text", "")
    rewritten = final_text != original_text

    if is_active:
        rs = bp.get("relevance_scoring") or {}
        passed = BulletPassedDetail(
            relevance_score=rs.get("score", 0),
            relevance_reason=rs.get("reason", ""),
        ) if rs else None
        trace_id = bp.get("trace_id", "")
        citations = (cite_map or {}).get(trace_id) or None
        return BulletDetailItem(
            trace_id=trace_id,
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=True,
            citations=citations,
            passed=passed,
        )

    discarded_citations = _resolve_citations(bp.get("citations") or [], source_lookup or {})

    rs = bp.get("relevance_scoring") or {}
    if rs and not rs.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="relevance_score",
                reason=rs.get("reason", ""),
                score=rs.get("score"),
            ),
        )

    eg_check = (bp.get("entity_grounding") or {}).get("check") or {}
    if eg_check.get("decision") == "invalid":
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="grounding",
                reason=eg_check.get("reason", ""),
            ),
        )

    ne = bp.get("novelty_embedding") or {}
    judgment = ne.get("judgment") or {}
    if judgment.get("decision") == "discard":
        _EMBEDDING_STRIP_KEYS = {"evidence_ids", "evidence"}
        clean_evaluators = []
        for ev in (judgment.get("evaluator_details") or []):
            ev_clean = {k: v for k, v in ev.items() if k not in _EMBEDDING_STRIP_KEYS}
            if "retrieved_bullets" in ev_clean:
                ev_clean["retrieved_bullets"] = [
                    {k: v for k, v in rb.items() if k not in _EMBEDDING_STRIP_KEYS}
                    for rb in (ev_clean["retrieved_bullets"] or [])
                ]
            clean_evaluators.append(ev_clean)
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_embedding",
                reason=judgment.get("reason", ""),
                evaluator_details=clean_evaluators,
            ),
        )

    emb_rc = ne.get("relevance_check") or {}
    if emb_rc and not emb_rc.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_embedding_relevance",
                reason=f"Rewritten bullet scored {emb_rc.get('score')} — below relevance threshold.",
                score=emb_rc.get("score"),
            ),
        )

    ns = bp.get("novelty_search") or {}
    search = ns.get("search") or {}
    if search.get("verdict") == "discard":
        details = search.get("details") or {}
        raw_verdicts = details.get("claim_verdicts") or []
        claims = details.get("claims") or []
        evidence_map: dict = details.get("evidence_map") or {}
        claim_details: list[ClaimVerdictDetail] = []
        for cv in raw_verdicts:
            idx = cv.get("claim_index", 0)
            claim_text = claims[idx].get("text", "") if idx < len(claims) else ""
            evidence = [
                EvidenceDetail(
                    simple_id=eid,
                    original_doc_id=(evidence_map.get(eid) or {}).get("original_doc_id", ""),
                    chunk_num=(evidence_map.get(eid) or {}).get("chunk_num", 0),
                    headline=(evidence_map.get(eid) or {}).get("headline", ""),
                    date=(evidence_map.get(eid) or {}).get("date", ""),
                    text=(evidence_map.get(eid) or {}).get("text", ""),
                )
                for eid in (cv.get("evidence_ids") or [])
            ]
            claim_details.append(ClaimVerdictDetail(
                claim_index=idx,
                claim_text=claim_text,
                novelty=cv.get("novelty", ""),
                evidence=evidence,
                reasoning=cv.get("reasoning", ""),
            ))
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_search",
                reason=search.get("reason") or "",
                claim_verdicts=claim_details or None,
                overall_verdict=search.get("overall_verdict"),
            ),
        )

    search_rc = ns.get("relevance_check") or {}
    if search_rc and not search_rc.get("passed", True):
        return BulletDetailItem(
            trace_id=bp.get("trace_id", ""),
            theme=bp.get("theme", ""),
            original_text=original_text,
            final_text=final_text if rewritten else None,
            is_active=False,
            citations=discarded_citations,
            discarded=BulletDiscardDetail(
                stage="novelty_search_relevance",
                reason=f"Search-rewritten bullet scored {search_rc.get('score')} — below relevance threshold.",
                score=search_rc.get("score"),
                evaluator_reasoning=search_rc.get("reasoning"),
            ),
        )

    failure = bp.get("failure") or {}
    return BulletDetailItem(
        trace_id=bp.get("trace_id", ""),
        theme=bp.get("theme", ""),
        original_text=original_text,
        final_text=bp.get("text", ""),
        is_active=False,
        discarded=BulletDiscardDetail(
            stage="error",
            reason=failure.get("error_message", "Unknown pipeline error"),
        ),
    )


def _parse_bullet_trace(bp: dict) -> BulletTrace:
    rs_raw = bp.get("relevance_scoring") or {}
    relevance_scoring = (
        RelevanceScoringTrace(
            score=rs_raw.get("score", 0),
            reason=rs_raw.get("reason", ""),
            passed=rs_raw.get("passed", False),
        )
        if rs_raw else None
    )

    eg_raw = (bp.get("entity_grounding") or {}).get("check") or {}
    grounding = (
        GroundingTrace(decision=eg_raw.get("decision", ""), reason=eg_raw.get("reason", ""))
        if eg_raw else None
    )

    ne_raw = bp.get("novelty_embedding") or {}
    j_raw = ne_raw.get("judgment") or {}
    rew_raw = ne_raw.get("rewrite") or {}
    rel_raw = ne_raw.get("relevance_check") or {}
    embedding = (
        EmbeddingTrace(
            judgment=EmbeddingJudgmentTrace(
                decision=j_raw.get("decision", ""),
                reason=j_raw.get("reason", ""),
                evaluator_details=j_raw.get("evaluator_details") or [],
            ) if j_raw else None,
            rewritten_text=rew_raw.get("text_after") if rew_raw else None,
            relevance_score=rel_raw.get("score") if rel_raw else None,
            relevance_passed=rel_raw.get("passed") if rel_raw else None,
        )
        if ne_raw else None
    )

    ns_raw = bp.get("novelty_search") or {}
    s_raw = ns_raw.get("search") or {}
    sr_raw = ns_raw.get("relevance_check") or {}
    search = (
        SearchTrace(
            verdict=s_raw.get("verdict", ""),
            rewritten_text=s_raw.get("rewritten_text"),
            duration_seconds=s_raw.get("duration_seconds"),
            reason=s_raw.get("reason"),
            details=s_raw.get("details"),
            relevance_score=sr_raw.get("score") if sr_raw else None,
            relevance_passed=sr_raw.get("passed") if sr_raw else None,
        )
        if s_raw else None
    )

    return BulletTrace(
        trace_id=bp.get("trace_id", ""),
        is_active=bp.get("is_active", True),
        theme=bp.get("theme", ""),
        text=bp.get("text", ""),
        citations=bp.get("citations") or [],
        relevance_scoring=relevance_scoring,
        grounding=grounding,
        embedding=embedding,
        search=search,
        failure=bp.get("failure"),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/reports/bullets",
    response_model=BatchBulletsResponse,
    dependencies=[Depends(require_api_key)],
    summary="Get published bullets for multiple entities, grouped by run",
    description=(
        "Returns the published bullet points for one or more entities, grouped by run and ordered "
        "newest-first. Pass an empty `entity_ids` list to retrieve all entities in the database.\n\n"
        "Use `max_runs` to limit how many runs are returned per entity: "
        "`1` for the latest run only, `N` for the last N runs, omit for all runs.\n\n"
        "Each bullet includes the final text, source citations, and novelty metadata "
        "(`search_action`, `not_fully_novel`). Discarded bullets are returned as counts "
        "grouped by stage (`discarded_by_relevance`, `discarded_by_grounding`, `discarded_by_novelty`)."
    ),
)
def get_bullets(body: BatchBulletsRequest) -> BatchBulletsResponse:
    engine = get_engine()
    storage = SQLiteGeneratedBulletPointStorage(engine)
    results: list[EntityBulletsResult] = []
    entity_ids = body.entity_ids or _all_entity_ids(engine)

    for entity_id in entity_ids:
        grouped = storage.get_all_runs_bullets(entity_id)

        if not grouped:
            results.append(_build_entity_result_from_run_log(entity_id, engine))
            continue

        # Apply max_runs limit (grouped is already ordered newest-first)
        run_ids_ordered = list(grouped.keys())
        if body.max_runs is not None:
            run_ids_ordered = run_ids_ordered[:body.max_runs]

        # Find recent runs with 0 active bullets that are missing from SQLGeneratedBulletPoint.
        # Without this, get_bullets would skip those runs and return stale data from older runs.
        _rlog_limit = body.max_runs if body.max_runs is not None else 50
        empty_runs = _get_empty_run_results_for_entity(engine, entity_id, _rlog_limit)

        run_info: dict[str, tuple[str, datetime, datetime]] = {
            run_id: (grouped[run_id][0].entity_id, grouped[run_id][0].report_window_start, grouped[run_id][0].report_window_end)
            for run_id in run_ids_ordered
        }
        discarded_map = _load_discarded_for_runs(run_info)

        entity_name: str | None = None
        runs: list[RunBulletsResult] = []

        for run_id in run_ids_ordered:
            rows = grouped[run_id]
            first = rows[0]
            if entity_name is None:
                entity_name = first.entity_name

            source_lookup = _source_lookup_from_output_json(
                engine, entity_id, first.report_window_start, first.report_window_end
            )

            bullets = [
                BulletPointItem(
                    trace_id=row.trace_id,
                    text=row.text,
                    citations=[
                        CitationDetail(
                            id=c["id"],
                            headline=c["headline"],
                            text=c["text"],
                            url=c.get("url"),
                            source_name=c.get("source_name") or source_lookup.get(c["id"], ""),
                        )
                        for c in (row.citations or [])
                    ],
                    embedding_decision=row.embedding_decision,
                    search_action=row.search_action,
                    not_fully_novel=row.not_fully_novel or False,
                )
                for row in rows
            ]

            discarded = discarded_map.get(run_id, {})
            discarded_relevance = discarded.get("relevance", [])
            discarded_grounding = discarded.get("grounding", [])
            discarded_novelty = discarded.get("novelty", [])
            bullets_discarded = len(discarded_relevance) + len(discarded_grounding) + len(discarded_novelty)
            runs.append(RunBulletsResult(
                run_id=run_id,
                report_window_start=first.report_window_start,
                report_window_end=first.report_window_end,
                run_created_at=first.created_at,
                bullet_count=len(bullets),
                bullets_saved=len(bullets),
                bullets_discarded=bullets_discarded,
                bullets=bullets,
                discarded_by_relevance=discarded_relevance,
                discarded_by_grounding=discarded_grounding,
                discarded_by_novelty=discarded_novelty,
            ))

        # Merge runs-with-bullets and empty runs, sort newest-first, apply max_runs.
        if empty_runs:
            all_runs: list[RunBulletsResult] = sorted(
                runs + empty_runs,
                key=lambda r: r.run_created_at.replace(tzinfo=timezone.utc)
                if r.run_created_at.tzinfo is None else r.run_created_at,
                reverse=True,
            )
            if body.max_runs is not None:
                all_runs = all_runs[:body.max_runs]
        else:
            all_runs = runs

        total_bullets = sum(r.bullet_count for r in all_runs)
        results.append(EntityBulletsResult(
            entity_id=entity_id,
            found=True,
            entity_name=entity_name,
            total_runs=len(all_runs),
            total_bullets=total_bullets,
            runs=all_runs,
        ))

    return BatchBulletsResponse(
        results=results,
        total_entities=len(results),
        total_bullets=sum(r.total_bullets for r in results),
    )


@router.post(
    "/reports/bullets/detail",
    response_model=BatchBulletsDetailResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_api_key)],
    summary="Full pipeline detail for multiple entities",
    description=(
        "Returns full pipeline detail for every bullet — both published and discarded — "
        "for one or more entities. Pass an empty `entity_ids` list to retrieve all entities.\n\n"
        "**Published bullets** include the relevance score and reasoning that justified publishing.\n\n"
        "**Discarded bullets** include the stage that eliminated them and the reason:\n"
        "- `relevance_score` — scored too low on financial materiality\n"
        "- `grounding` — text not verifiable against cited sources\n"
        "- `novelty_embedding` — already reported in a previous run\n"
        "- `novelty_search` — per-claim verdicts with the evidence chunks that already covered the information"
    ),
)
def get_bullets_detail(body: BatchBulletsDetailRequest) -> BatchBulletsDetailResponse:
    engine = get_engine()
    results: list[EntityDetailResult] = []
    entity_ids = body.entity_ids or _all_entity_ids(engine)

    for entity_id in entity_ids:
        with Session(engine) as session:
            orch = session.get(SQLEntityOrchestrationState, entity_id)
            entity_name: str | None = orch.kg_name if orch else None

            query = (
                select(SQLEntityPipelineRunLog)
                .where(SQLEntityPipelineRunLog.entity_id == entity_id)
                .where(SQLEntityPipelineRunLog.status == "succeeded")
            )
            if body.from_date is not None:
                query = query.where(SQLEntityPipelineRunLog.report_window_end >= body.from_date)
            if body.to_date is not None:
                query = query.where(SQLEntityPipelineRunLog.report_window_start <= body.to_date)
            run_rows = session.exec(
                query.order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
            ).all()

        if not run_rows:
            results.append(EntityDetailResult(entity_id=entity_id, found=False))
            continue

        entity_runs: list[RunDetailResult] = []
        for row in run_rows:
            raw_bullets: list[dict] = []
            raw_source_refs: dict = {}
            if row.output_json:
                try:
                    parsed = json.loads(row.output_json)
                    if isinstance(parsed, list):
                        raw_bullets = parsed
                    else:
                        raw_bullets = parsed.get("bullet_points") or []
                        raw_source_refs = parsed.get("source_references") or {}
                except (json.JSONDecodeError, TypeError):
                    pass

            source_lookup: dict[str, dict] = {}
            for src in raw_source_refs.values():
                if isinstance(src, dict):
                    doc_id = src.get("document_id")
                    chunk_id = src.get("chunk_id")
                    if doc_id is not None and chunk_id is not None:
                        source_lookup[f"CQS:{doc_id}-{chunk_id}"] = src

            active_trace_ids = [
                bp.get("trace_id") for bp in raw_bullets
                if bp.get("is_active") and bp.get("trace_id")
            ]
            cite_map: dict[str, list[CitationDetail]] = {}
            if active_trace_ids:
                with Session(engine) as session:
                    cite_rows = session.exec(
                        select(SQLGeneratedBulletPoint).where(
                            SQLGeneratedBulletPoint.trace_id.in_(active_trace_ids)
                        )
                    ).all()
                cite_map = {
                    r.trace_id: [
                        CitationDetail(id=c["id"], headline=c["headline"], text=c["text"], url=c.get("url"), source_name=c.get("source_name", ""))
                        for c in (r.citations or [])
                        if isinstance(c, dict)
                    ]
                    for r in cite_rows
                    if r.trace_id
                }

            bullets = [_build_bullet_detail(bp, cite_map, source_lookup) for bp in raw_bullets]
            active = sum(1 for b in bullets if b.is_active)
            entity_runs.append(RunDetailResult(
                run_id=str(row.run_id),
                report_window_start=row.report_window_start,
                report_window_end=row.report_window_end,
                total_bullets=len(bullets),
                active_bullets=active,
                discarded_bullets=len(bullets) - active,
                bullets=bullets,
            ))

        results.append(EntityDetailResult(
            entity_id=entity_id,
            found=True,
            entity_name=entity_name,
            runs=entity_runs,
        ))

    return BatchBulletsDetailResponse(
        results=results,
        total_entities=len(results),
    )


@router.get(
    "/reports/runs/{run_id}/trace",
    response_model=RunTraceResponse,
    dependencies=[Depends(require_api_key)],
    summary="Full per-bullet step trace for a run",
    description=(
        "Returns the complete pipeline trace for every bullet processed in a run: "
        "relevance score, grounding decision, embedding novelty judgment, "
        "search novelty verdict, and post-search relevance check — "
        "including bullets that were discarded along the way (`is_active=false`).\n\n"
        "Available only after the run has completed (status `succeeded` or `failed`). "
        "Returns 404 if the run does not exist, 409 if it is still running, "
        "and 204 if it completed but produced no bullet trace."
    ),
)
def get_run_trace(run_id: uuid.UUID) -> RunTraceResponse:
    with Session(get_engine()) as session:
        row = session.get(SQLEntityPipelineRunLog, run_id)

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found.")
    if row.status == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Run {run_id} is still in progress.")
    if not row.output_json:
        raise HTTPException(
            status_code=status.HTTP_204_NO_CONTENT,
            detail=f"Run {run_id} completed but has no bullet trace (early exit or no bullets).",
        )

    try:
        raw_bullets: list[dict] = json.loads(row.output_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Trace data for run {run_id} is malformed: {exc}",
        )

    bullets = [_parse_bullet_trace(bp) for bp in raw_bullets]
    return RunTraceResponse(
        run_id=str(row.run_id),
        entity_id=row.entity_id,
        total_bullets=len(bullets),
        active_bullets=sum(1 for b in bullets if b.is_active),
        bullets=bullets,
    )


# ── Narratives ────────────────────────────────────────────────────────────────


@router.post(
    "/reports/narratives",
    response_model=BatchNarrativesResponse,
    dependencies=[Depends(require_api_key)],
    summary="Retrieve per-entity editorial narratives",
    description=(
        "Returns the editorial narratives generated after each pipeline run. "
        "Each narrative is a 2-3 sentence summary of **all active bullets published "
        "for that entity on the same UTC calendar day** (not just the bullets from "
        "the triggering run). Only present when `generate_narrative: true` was passed "
        "to the `run-parallel` call.\n\n"
        "Multiple narrative rows can exist for the same entity and day (one per run). "
        "Results are sorted newest first; the first entry for a given date is the most "
        "up-to-date summary.\n\n"
        "Filter by `from_date` / `to_date` (ISO 8601) to narrow the date range. "
        "If `entity_ids` is empty, all entities in the database are returned."
    ),
)
def get_narratives(body: BatchNarrativesRequest) -> BatchNarrativesResponse:
    engine = get_engine()
    if body.universe and body.entity_ids:
        raise HTTPException(status_code=422, detail="Provide either 'entity_ids' or 'universe', not both.")
    if body.universe:
        if body.universe == "my_portfolio":
            entity_ids = _get_my_portfolio_ids()
        else:
            entity_ids = _UNIVERSES.get(body.universe)
        if entity_ids is None:
            raise HTTPException(status_code=404, detail=f"Universe '{body.universe}' not found. Available: {list(_UNIVERSES) + ['my_portfolio']}")
    else:
        entity_ids = body.entity_ids or _all_entity_ids(engine)

    results: list[EntityNarrativesResult] = []
    with Session(engine) as session:
        for eid in entity_ids:
            q = select(SQLRunNarrative).where(SQLRunNarrative.entity_id == eid)
            if body.from_date:
                q = q.where(SQLRunNarrative.report_date >= body.from_date)
            if body.to_date:
                q = q.where(SQLRunNarrative.report_date <= body.to_date)
            q = q.order_by(desc(SQLRunNarrative.created_at))
            rows = session.exec(q).all()
            results.append(EntityNarrativesResult(
                entity_id=eid,
                found=len(rows) > 0,
                narratives=[
                    NarrativeItem(
                        run_id=str(r.run_id),
                        report_date=r.report_date,
                        narrative_text=r.narrative_text,
                        bullets_count=r.bullets_count,
                        created_at=r.created_at,
                    )
                    for r in rows
                ],
            ))

    return BatchNarrativesResponse(
        results=results,
        total_entities=len(results),
    )

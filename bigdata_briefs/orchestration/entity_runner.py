"""Run the LangGraph pipeline for one entity with orchestration state and audit log."""

from __future__ import annotations

import json
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Semaphore
from typing import Any

import httpx
from sqlalchemy import desc
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine, select

from bigdata_briefs.debug_logger import DebugLogger
from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.models import Entity, ReportDates
from bigdata_briefs.novelty.chunk_filter import ChunkFilterService
from bigdata_briefs.novelty.storage import (
    ChunkHashStorage,
    SQLiteEmbeddingStorage,
    SQLiteGeneratedBulletPointStorage,
)
from bigdata_briefs.orchestration.db import ensure_orchestration_schema
from bigdata_briefs.orchestration.kg_entities import entity_from_kg_record, fetch_kg_entities_by_ids
from bigdata_briefs.orchestration.models import SQLBulletRunLog, SQLEntityOrchestrationState, SQLEntityPipelineRunLog, SQLRunMetrics
from bigdata_briefs.orchestration.output import fetch_new_novelty_ok_bullets, fetch_previous_bullets
from bigdata_briefs.orchestration.windows import WindowEndNotAfterStartError, WindowMode, build_report_dates_for_entity_run
from bigdata_briefs.graph.dependencies import RuntimeDependencies
from bigdata_briefs.graph.graph import compile_brief_graph
from bigdata_briefs.graph.state import make_empty_state_defaults
from bigdata_briefs.query_service.api import APIQueryService
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.service import BriefPipelineService
from bigdata_briefs.settings import settings


class OrchestratorEntityBusyError(RuntimeError):
    """Another non-stale run is in progress for this entity."""


class EntityResolutionError(RuntimeError):
    """Entity missing from KG and from orchestration SQLite KG cache."""


def _entity_pair_from_kg_record(
    rec: dict[str, Any],
    *,
    source: str,
    kg_for_persist: dict[str, Any] | None,
) -> tuple[Entity, dict[str, Any] | None]:
    try:
        return entity_from_kg_record(rec), kg_for_persist
    except ValueError as e:
        raise EntityResolutionError(f"Invalid Knowledge Graph record ({source}): {e}") from e


@dataclass
class EntityRunResult:
    entity_id: str
    report_dates: ReportDates
    success: bool
    dry_run: bool
    previous_bullets: list[dict[str, Any]] = field(default_factory=list)
    new_bullets_novelty_ok: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    pipeline_step_results: dict[str, bool] | None = None
    run_id: uuid.UUID | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_aware(dt: datetime) -> datetime:
    """SQLite often returns naive UTC timestamps; normalize for comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_or_create_orch_row(session: Session, entity_id: str) -> SQLEntityOrchestrationState:
    row = session.get(SQLEntityOrchestrationState, entity_id)
    if row is None:
        row = SQLEntityOrchestrationState(entity_id=entity_id)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _finalize_stale_running(
    session: Session,
    *,
    entity_id: str,
    now: datetime,
    stale_seconds: int,
) -> None:
    stmt = (
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.entity_id == entity_id)
        .where(SQLEntityPipelineRunLog.status == "running")
        .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
    )
    row = session.exec(stmt).first()
    if row is None:
        return
    age = (now - _as_utc_aware(row.process_started_at_utc)).total_seconds()
    if age >= stale_seconds:
        row.status = "failed"
        row.process_completed_at_utc = now
        row.error_summary = "stale running lease cleared"
        session.add(row)
        session.commit()


def _assert_no_active_run(
    session: Session,
    *,
    entity_id: str,
    now: datetime,
    stale_seconds: int,
) -> None:
    stmt = (
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.entity_id == entity_id)
        .where(SQLEntityPipelineRunLog.status == "running")
        .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
    )
    row = session.exec(stmt).first()
    if row is None:
        return
    age = (now - _as_utc_aware(row.process_started_at_utc)).total_seconds()
    if age < stale_seconds:
        raise OrchestratorEntityBusyError(
            f"entity_id={entity_id!r} has status=running since {row.process_started_at_utc!r}"
        )


class OrchestratorWindowOverlapError(RuntimeError):
    """Requested window overlaps a completed run for this entity."""


def _assert_no_overlapping_run(
    session: Session,
    *,
    entity_id: str,
    report_dates: ReportDates,
) -> None:
    """Raise if any completed run for entity_id overlaps [report_dates.start, report_dates.end)."""
    stmt = (
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.entity_id == entity_id)
        .where(SQLEntityPipelineRunLog.status.in_(["succeeded", "no_data"]))
    )
    rows = session.exec(stmt).all()
    rs = _as_utc_aware(report_dates.start)
    re = _as_utc_aware(report_dates.end)
    for row in rows:
        existing_start = _as_utc_aware(row.report_window_start)
        existing_end = _as_utc_aware(row.report_window_end)
        if rs < existing_end and existing_start < re:
            raise OrchestratorWindowOverlapError(
                f"entity_id={entity_id!r} requested window [{rs.isoformat()}, {re.isoformat()}) "
                f"overlaps existing run [{existing_start.isoformat()}, {existing_end.isoformat()})"
            )


def _insert_running_log(
    session: Session,
    *,
    entity_id: str,
    report_dates: ReportDates,
    now: datetime,
    run_id: uuid.UUID | None = None,
) -> SQLEntityPipelineRunLog:
    log = SQLEntityPipelineRunLog(
        run_id=run_id or uuid.uuid4(),
        entity_id=entity_id,
        report_window_start=report_dates.start,
        report_window_end=report_dates.end,
        process_started_at_utc=now,
        process_completed_at_utc=None,
        status="running",
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def _update_run_log_end(
    session: Session,
    log: SQLEntityPipelineRunLog,
    *,
    now: datetime,
    status: str,
    error_summary: str | None = None,
    exit_code: int | None = None,
    output_json: str | None = None,
) -> None:
    log.process_completed_at_utc = now
    log.status = status
    log.error_summary = error_summary
    log.exit_code = exit_code
    if output_json is not None:
        log.output_json = output_json
    session.add(log)
    session.commit()


def _apply_success_orchestration_state(
    session: Session,
    orch: SQLEntityOrchestrationState,
    *,
    report_dates: ReportDates,
    kg_record: dict[str, Any] | None,
    now: datetime,
) -> None:
    orch.last_window_end = report_dates.end
    orch.updated_at = now
    if kg_record is not None:
        orch.kg_name = kg_record.get("name")
        orch.kg_category = kg_record.get("category")
        listings = kg_record.get("listing_values")
        from bigdata_briefs.orchestration.kg_entities import ticker_from_listing_values

        orch.kg_ticker = ticker_from_listing_values(
            listings if isinstance(listings, list) else None
        )
        orch.kg_payload_json = json.dumps(kg_record)
        orch.kg_fetched_at = now
    session.add(orch)
    session.commit()


def resolve_entity_for_run(
    *,
    entity_id: str,
    orch: SQLEntityOrchestrationState,
    refresh_entity: bool,
    kg_precache: dict[str, dict[str, Any]] | None = None,
    rate_limiter: RequestsPerMinuteController | None = None,
) -> tuple[Entity, dict[str, Any] | None]:
    """
    Returns ``(Entity, kg_record_or_none)``.

    Resolution order: batch precache (same run), orchestration ``kg_payload_json`` from a
    prior successful run, then live Knowledge Graph. There is no CLI/manual entity build:
    if Graph and cache both fail, raises ``EntityResolutionError``.

    ``kg_record`` is returned when a fresh KG response should be persisted on success.
    """
    if not refresh_entity and kg_precache and entity_id in kg_precache:
        raw = kg_precache[entity_id]
        if not isinstance(raw, dict):
            raise EntityResolutionError(
                f"KG precache entry for entity_id={entity_id!r} must be a dict, got {type(raw).__name__}"
            )
        return _entity_pair_from_kg_record(raw, source="batch_precache", kg_for_persist=raw)
    if not refresh_entity and orch.kg_payload_json:
        try:
            loaded = json.loads(orch.kg_payload_json)
        except json.JSONDecodeError as e:
            raise EntityResolutionError(
                f"Orchestration kg_payload_json is invalid JSON for entity_id={entity_id!r}: {e}"
            ) from e
        if not isinstance(loaded, dict):
            raise EntityResolutionError(
                f"Orchestration kg_payload_json must decode to an object for entity_id={entity_id!r}, "
                f"got {type(loaded).__name__}"
            )
        return _entity_pair_from_kg_record(
            loaded,
            source="orchestration_sqlite_cache",
            kg_for_persist=None,
        )
    try:
        results = fetch_kg_entities_by_ids(
            [entity_id],
            api_key=str(settings.BIGDATA_API_KEY),
            base_url=settings.API_BASE_URL,
            timeout_seconds=settings.API_TIMEOUT_SECONDS,
            rate_limiter=rate_limiter,
        )
    except Exception as e:
        raise EntityResolutionError(
            f"Knowledge Graph request failed for entity_id={entity_id!r} "
            f"(no usable row in orchestration SQLite cache): {e}"
        ) from e
    rec = results[entity_id]
    return _entity_pair_from_kg_record(rec, source="live_kg_response", kg_for_persist=rec)


def build_runtime_dependencies(
    engine: Engine,
    *,
    rate_limiter: RequestsPerMinuteController | None = None,
    connection_sem: Semaphore | None = None,
    http_client: httpx.Client | None = None,
) -> RuntimeDependencies:
    """Build all service dependencies for a graph run.

    When ``rate_limiter`` / ``connection_sem`` / ``http_client`` are provided,
    the resulting ``APIQueryService`` shares those singletons with every other
    concurrent entity run — enforcing a single process-global 450 QPM budget.
    """
    embedding_storage = SQLiteEmbeddingStorage(engine)
    generated_bullet_storage = SQLiteGeneratedBulletPointStorage(engine)
    chunk_hash_storage = ChunkHashStorage(engine)
    chunk_filter_service = ChunkFilterService(chunk_hash_storage)
    query_service = APIQueryService(
        chunk_filter_service=chunk_filter_service,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )
    brief_service = BriefPipelineService.factory(embedding_storage=embedding_storage)
    return RuntimeDependencies(
        engine=engine,
        query_service=query_service,
        llm_client=brief_service.llm_client,
        brief_service=brief_service,
        novelty_service=brief_service.novelty_filter_service,
        embedding_client=brief_service.novelty_filter_service.embedding_client,
        embedding_storage=embedding_storage,
        generated_bullet_storage=generated_bullet_storage,
        # Forward so graph nodes with their own HTTP paths can share the
        # same 450 QPM budget as APIQueryService._call_api.
        bigdata_rate_limiter=query_service.rate_limit_controller,
    )


def _node_metrics_to_step_summary(node_metrics: list[dict]) -> dict[str, bool]:
    """Convert graph node_metrics to a node_id -> success bool dict."""
    return {
        rec["node_id"]: (rec.get("error_count", 0) == 0)
        for rec in node_metrics
        if "node_id" in rec
    }


def _get_discard_stage(bp: dict) -> str | None:
    """Return the pipeline stage that discarded this bullet, or None if active."""
    if bp.get("is_active", True):
        return None
    rs = bp.get("relevance_scoring") or {}
    if rs and not rs.get("passed", True):
        return "relevance_score"
    eg = (bp.get("entity_grounding") or {}).get("check") or {}
    if eg.get("decision") == "invalid":
        return "grounding"
    ne = bp.get("novelty_embedding") or {}
    j = ne.get("judgment") or {}
    if j.get("decision") == "discard":
        return "novelty_embedding"
    rc = ne.get("relevance_check") or {}
    if rc and not rc.get("passed", True):
        return "novelty_embedding_relevance"
    ns = bp.get("novelty_search") or {}
    s = ns.get("search") or {}
    if s.get("verdict") == "discard":
        return "novelty_search"
    src = ns.get("relevance_check") or {}
    if src and not src.get("passed", True):
        return "novelty_search_relevance"
    return "unknown"


def _build_doc_index(source_refs: dict) -> dict:
    """Build {document_id[-chunk_id]: {headline,text,source_name,date}} from source_references."""
    index: dict = {}
    for ref in source_refs.values():
        doc_id = str(ref.get("document_id") or "").strip()
        chunk_id = ref.get("chunk_id")
        if not doc_id:
            continue
        ts = str(ref.get("ts") or "").replace("T", " ")[:19]
        entry = {
            "headline": ref.get("headline") or "",
            "text": ref.get("text") or "",
            "source_name": ref.get("source_name") or "",
            "date": ts,
        }
        if chunk_id is not None:
            index.setdefault(f"{doc_id}-{chunk_id}", entry)
        index.setdefault(doc_id, entry)
    return index


def _resolve_citation(cit_id: str, doc_index: dict) -> dict:
    tail = cit_id.split(":", 1)[-1]
    meta = doc_index.get(tail) or doc_index.get(tail.rsplit("-", 1)[0]) or {}
    return {
        "id": cit_id,
        "headline": meta.get("headline") or "",
        "text": meta.get("text") or "",
        "source_name": meta.get("source_name") or "",
        "date": meta.get("date") or "",
    }


def _flush_bullet_run_log(eng: Engine, run_id: uuid.UUID, entity_id: str, final_state: dict) -> None:
    """Write one SQLBulletRunLog row per bullet, storing all display data so the UI
    never needs to parse output_json again."""
    bullet_points: list[dict] = final_state.get("bullet_points") or []
    if not bullet_points:
        return

    source_refs: dict = final_state.get("source_references") or {}
    doc_index = _build_doc_index(source_refs)
    now = datetime.now(timezone.utc)
    rows: list[SQLBulletRunLog] = []

    for bp in bullet_points:
        is_active: bool = bp.get("is_active", True)
        ne = bp.get("novelty_embedding") or {}
        ns_block = bp.get("novelty_search") or {}
        s = ns_block.get("search") or {}
        search_details = s.get("details") or {}
        rs = bp.get("relevance_scoring") or {}
        eg = (bp.get("entity_grounding") or {}).get("check") or {}
        gen = bp.get("generation") or {}
        j = ne.get("judgment") or {}

        overall_verdict = s.get("overall_verdict")
        # mixed / mixed_partial: old context explicitly in the subordinate clause → amber
        # mixed_noise / single_partially_novel: result is fully novel after rewrite → green
        not_fully_novel = bool(is_active and overall_verdict in ("mixed", "mixed_partial"))

        ne_rewrite = (ne.get("rewrite") or {}).get("text_after")
        search_rewrite = s.get("rewritten_text")
        final_text = search_rewrite or ne_rewrite or bp.get("text", "")

        # ── citations: resolve IDs → full metadata via doc_index ─────────────
        citations = [
            _resolve_citation(str(cid), doc_index)
            for cid in (bp.get("citations") or [])
        ]

        # ── evaluator details (novelty embedding) ─────────────────────────────
        evaluator_details = j.get("evaluator_details") or []

        # ── novelty search claim verdicts + evidence map ──────────────────────
        claim_verdicts = search_details.get("claim_verdicts") or []
        evidence_map = search_details.get("evidence_map") or {}

        # ── grounding citation IDs ────────────────────────────────────────────
        grounding_citations = (
            [str(c) for c in (bp.get("citations") or [])]
            if _get_discard_stage(bp) == "grounding" else []
        )

        rows.append(SQLBulletRunLog(
            run_id=run_id,
            entity_id=entity_id,
            trace_id=str(bp.get("trace_id") or ""),
            is_active=is_active,
            not_fully_novel=not_fully_novel,
            discard_stage=_get_discard_stage(bp),
            text=final_text,
            original_text=str(gen.get("original_text") or bp.get("text") or ""),
            theme=str(bp.get("theme") or ""),
            relevance_score=rs.get("score"),
            relevance_passed=rs.get("passed"),
            relevance_reason=rs.get("reason"),
            grounding_decision=eg.get("decision"),
            grounding_reason=eg.get("reason"),
            embedding_decision=j.get("decision"),
            embedding_reason=j.get("reason"),
            embedding_rewritten=bool(ne.get("rewrite")),
            search_verdict=s.get("verdict"),
            search_overall_verdict=overall_verdict,
            search_reason=s.get("reason"),
            search_duration_seconds=s.get("duration_seconds"),
            search_relevance_score=(ns_block.get("relevance_check") or {}).get("score"),
            search_relevance_reason=(ns_block.get("relevance_check") or {}).get("reasoning"),
            citations_json=json.dumps(citations, default=str),
            evaluator_details_json=json.dumps(evaluator_details, default=str),
            claim_verdicts_json=json.dumps(claim_verdicts, default=str),
            evidence_map_json=json.dumps(evidence_map, default=str),
            grounding_citations_json=json.dumps(grounding_citations),
            created_at=now,
        ))

    try:
        with Session(eng) as session:
            session.add_all(rows)
            session.commit()
    except Exception:
        pass  # never let bullet log failures break the run


def _flush_run_metrics(
    eng: Engine,
    run_id: uuid.UUID,
    entity_id: str,
    report_dates: ReportDates,
    entity_metrics: EntityStepMetrics,
) -> None:
    """Write one SQLRunMetrics row for the completed run.

    Reads the already-accumulated data from ``entity_metrics`` — no LLM calls
    are made here. Silently swallowed on failure so it never breaks the run.
    """
    try:
        totals = entity_metrics.get_totals()
        emb = entity_metrics.get_embedding_summary()
        step_summary = entity_metrics.get_step_summary()
        llm_models = entity_metrics.get_llm_model_summary()

        with entity_metrics.lock:
            chunks_total = entity_metrics._total_chunks

        # Use the per-entity embedding accumulator for the totals so the cost
        # is correct even when embeddings were tracked outside a named step.
        total_llm_cost = totals["total_llm_cost_usd"]
        total_emb_cost = round(emb["cost_usd"], 6)

        row = SQLRunMetrics(
            run_id=run_id,
            entity_id=entity_id,
            report_window_start=report_dates.start,
            report_window_end=report_dates.end,
            llm_per_model_json=json.dumps(llm_models, default=str),
            embedding_model=emb["model"],
            embedding_tokens=emb["tokens"],
            embedding_cost_usd=total_emb_cost,
            chunks_total=chunks_total,
            step_detail_json=json.dumps(step_summary, default=str),
            total_llm_cost_usd=total_llm_cost,
            total_embedding_cost_usd=total_emb_cost,
            total_cost_usd=round(total_llm_cost + total_emb_cost, 6),
            created_at=datetime.now(timezone.utc),
        )
        with Session(eng) as session:
            session.add(row)
            session.commit()
    except Exception:
        pass  # never let metrics flush failures break the run


def run_entity_incremental(
    *,
    entity_id: str,
    pipeline_config: dict[str, Any],
    state_dir: Path,
    refresh_entity: bool = False,
    dry_run: bool = False,
    force_window_start: datetime | None = None,
    force_window_end: datetime | None = None,
    force_run: bool = False,
    window_mode: WindowMode = WindowMode.DAILY,
    engine: Engine | None = None,
    kg_precache: dict[str, dict[str, Any]] | None = None,
    run_id: uuid.UUID | None = None,
    rate_limiter: RequestsPerMinuteController | None = None,
    connection_sem: Semaphore | None = None,
    http_client: httpx.Client | None = None,
) -> EntityRunResult:
    """
    Compute window, optional dry-run, else acquire lease, run the LangGraph pipeline,
    update orchestration row + run log on success (including KG cache when freshly fetched).

    A run is considered successful when ``pipeline_status`` is ``"completed"`` (bullets
    produced) or ``"no_data"`` (window searched, nothing found or all bullets discarded).
    In both cases the report window is advanced so the same window is never re-queried.
    """
    eng = engine or create_engine(settings.DB_STRING, echo=False)
    ensure_orchestration_schema(eng)
    now = _utc_now()
    last_window_end: datetime | None = None

    with Session(eng) as session:
        orch = _get_or_create_orch_row(session, entity_id)
        last_window_end = orch.last_window_end
        if not dry_run:
            if force_run:
                _finalize_stale_running(
                    session,
                    entity_id=entity_id,
                    now=now,
                    stale_seconds=0,
                )
            else:
                _finalize_stale_running(
                    session,
                    entity_id=entity_id,
                    now=now,
                    stale_seconds=settings.ORCHESTRATION_STALE_RUNNING_SECONDS,
                )
            _assert_no_active_run(
                session,
                entity_id=entity_id,
                now=now,
                stale_seconds=settings.ORCHESTRATION_STALE_RUNNING_SECONDS,
            )

    if force_window_start is not None and force_window_end is not None:
        rs, re_ = force_window_start, force_window_end
        if rs.tzinfo is None:
            rs = rs.replace(tzinfo=timezone.utc)
        if re_.tzinfo is None:
            re_ = re_.replace(tzinfo=timezone.utc)
        if re_ <= rs:
            return EntityRunResult(
                entity_id=entity_id,
                report_dates=ReportDates(start=rs, end=re_),
                success=False,
                dry_run=dry_run,
                error="force window invalid: end must be after start",
            )
        report_dates = ReportDates(start=rs, end=re_)
    else:
        try:
            report_dates = build_report_dates_for_entity_run(
                now=now,
                last_window_end=last_window_end,
                window_mode=window_mode,
            )
        except WindowEndNotAfterStartError as e:
            return EntityRunResult(
                entity_id=entity_id,
                report_dates=ReportDates(start=now, end=now),
                success=False,
                dry_run=dry_run,
                error=str(e),
            )

    with Session(eng) as session:
        try:
            _assert_no_overlapping_run(session, entity_id=entity_id, report_dates=report_dates)
        except OrchestratorWindowOverlapError as e:
            return EntityRunResult(
                entity_id=entity_id,
                report_dates=report_dates,
                success=False,
                dry_run=dry_run,
                error=str(e),
            )

    if dry_run:
        prev = fetch_previous_bullets(eng, entity_id, report_dates)
        return EntityRunResult(
            entity_id=entity_id,
            report_dates=report_dates,
            success=True,
            dry_run=True,
            previous_bullets=prev,
            new_bullets_novelty_ok=[],
        )

    with Session(eng) as session:
        orch_row = _get_or_create_orch_row(session, entity_id)
    entity, kg_record = resolve_entity_for_run(
        entity_id=entity_id,
        orch=orch_row,
        refresh_entity=refresh_entity,
        kg_precache=kg_precache,
        rate_limiter=rate_limiter,
    )

    with Session(eng) as session:
        run_log = _insert_running_log(
            session,
            entity_id=entity_id,
            report_dates=report_dates,
            now=now,
            run_id=run_id,
        )

    state_dir.mkdir(parents=True, exist_ok=True)
    deps = build_runtime_dependencies(
        eng,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
    )

    req = uuid.uuid4()
    deps.debug_logger = DebugLogger(
        request_id=req,
        report_start_date=report_dates.start.isoformat(),
        report_end_date=report_dates.end.isoformat(),
        entity_name=entity.name,
        base_dir=state_dir,
    )
    deps.entity_metrics = EntityStepMetrics(entity.name)

    initial_state = {
        **make_empty_state_defaults(),
        "entity_id": entity.id,
        "entity_name": entity.name,
        "entity_type": entity.entity_type,
        "entity_ticker": entity.ticker or "",
        "report_start_date": report_dates.start.isoformat(),
        "report_end_date": report_dates.end.isoformat(),
        "request_id": str(req),
        "config": pipeline_config,
    }
    graph_config = {"configurable": {"deps": deps}}

    pipeline_ok = False
    step_summary: dict[str, bool] = {}
    err: str | None = None
    err_tb: str | None = None
    final_state: dict = {}
    try:
        graph = compile_brief_graph()
        final_state = graph.invoke(initial_state, graph_config)
        status = final_state.get("pipeline_status", "")
        # "completed" = bullets produced; "no_data" = window searched, nothing to report
        # (includes early exits and cases where all bullets were discarded during processing)
        # "running" (no exception) = graph exited via a no_data conditional edge before any
        # node could update the status (e.g. _route_after_bullet_subgraph).
        # All three advance the window so the same range is never re-queried.
        pipeline_ok = status in ("completed", "no_data", "running")
        step_summary = _node_metrics_to_step_summary(
            final_state.get("node_metrics") or []
        )
    except Exception as e:
        err = str(e)
        err_tb = traceback.format_exc()
        pipeline_ok = False

    done = _utc_now()

    # Serialize the full bullet trace (all step metadata) for later inspection.
    bullet_trace_json: str | None = None
    if final_state:
        import json as _json
        try:
            bullet_trace_json = _json.dumps(
                {
                    "bullet_points": final_state.get("bullet_points") or [],
                    "source_references": final_state.get("source_references") or {},
                },
                default=str,
            )
        except Exception:
            bullet_trace_json = None

    with Session(eng) as session:
        log = session.get(SQLEntityPipelineRunLog, run_log.run_id) or session.merge(run_log)
        orch = _get_or_create_orch_row(session, entity_id)
        if pipeline_ok:
            _update_run_log_end(
                session,
                log,
                now=done,
                status="succeeded",
                exit_code=0,
                output_json=bullet_trace_json,
            )
            _apply_success_orchestration_state(
                session,
                orch,
                report_dates=report_dates,
                kg_record=kg_record,
                now=done,
            )
        else:
            full_error = err or "pipeline step failed"
            if err_tb:
                full_error = f"{full_error}\n\n{err_tb}"
            _update_run_log_end(
                session,
                log,
                now=done,
                status="failed",
                error_summary=full_error,
                exit_code=1,
                output_json=bullet_trace_json,  # preserve partial state even on failure
            )

    if final_state and pipeline_ok:
        _flush_bullet_run_log(eng, run_log.run_id, entity_id, final_state)

    if deps.entity_metrics is not None:
        _flush_run_metrics(eng, run_log.run_id, entity_id, report_dates, deps.entity_metrics)

    previous = fetch_previous_bullets(eng, entity_id, report_dates)
    new_ok: list[dict[str, Any]] = []
    if pipeline_ok:
        new_ok = fetch_new_novelty_ok_bullets(eng, entity_id, report_dates)

    return EntityRunResult(
        entity_id=entity_id,
        report_dates=report_dates,
        success=pipeline_ok,
        dry_run=False,
        previous_bullets=previous,
        new_bullets_novelty_ok=new_ok,
        error=None if pipeline_ok else (err or "pipeline failed"),
        pipeline_step_results=step_summary,
        run_id=run_log.run_id,
    )

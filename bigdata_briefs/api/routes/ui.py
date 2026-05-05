"""
UI routes — HTMX-powered browser interface.

Pages (full HTML):
    GET  /ui/run              → Run Brief (form + live results)
    GET  /ui/history          → Company History (clean, passed bullets only)
    GET  /ui/history-details  → Company History (full detail + discards)
    GET  /ui/admin            → Admin: reset DB / delete entity data

HTMX partials (HTML fragments):
    POST /ui/batch/run                → trigger batch; returns progress fragment
    POST /ui/batch/stop               → set cancel event for a running batch
    GET  /ui/partials/run-status      → live progress / final results (polled every 3s)
    GET  /ui/partials/history         → bullet history for a selected entity
    GET  /ui/partials/history-details → bullet history + full details for a selected entity
    POST /ui/admin/reset-db           → drop + recreate all tables
    POST /ui/admin/delete-entity      → delete all data for a specific entity
"""

from __future__ import annotations

import html
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc
from sqlmodel import Session, select

from sqlalchemy import delete as sa_delete, and_

from bigdata_briefs import logger
from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_engine,
    get_entity_executor,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.api.routes.universes import _UNIVERSES, _UNIVERSES_DIR
from bigdata_briefs.api.routes.scan import (
    build_scan_windows,
    db_cancel_scan,
    db_create_scan,
    db_get_scan,
    resolve_scan_start,
    run_scan_worker,
)
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import (
    SQLBatchParallelRun,
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLRunMetrics,
    SQLUIBatchRun,
    SQLUIScanRun,
)
from bigdata_briefs.pricing import calculate_chunk_cost
from bigdata_briefs.orchestration.windows import WindowMode, build_report_dates_for_entity_run
from bigdata_briefs.settings import settings



router = APIRouter(tags=["ui"])


# ── Batch state ───────────────────────────────────────────────────────────────


@dataclass
class EntityRunStatus:
    entity_id: str
    entity_name: str
    status: str  # running | succeeded | failed | cancelled | skipped
    error: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    run_id: str | None = None  # SQLEntityPipelineRunLog.run_id — used to load bullets from SQLBulletRunLog


def _get_entity_name(engine, entity_id: str) -> str:
    with Session(engine) as session:
        row = session.get(SQLEntityOrchestrationState, entity_id)
        if row and row.kg_name:
            return row.kg_name
    return entity_id


# ── DB helpers for batch persistence ─────────────────────────────────────────


def _db_create_batch(engine, batch_id: str, entity_ids: list[str]) -> None:
    now = datetime.now(timezone.utc)
    row = SQLUIBatchRun(
        batch_id=batch_id,
        status="running",
        entity_ids_json=json.dumps(entity_ids),
        results_json="[]",
        total=len(entity_ids),
        done=0,
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()


def _db_append_result(engine, batch_id: str, result: EntityRunStatus) -> None:
    with Session(engine) as session:
        row = session.get(SQLUIBatchRun, batch_id)
        if row is None:
            return
        existing: list[dict] = json.loads(row.results_json)
        existing.append({
            "entity_id": result.entity_id,
            "entity_name": result.entity_name,
            "status": result.status,
            "error": result.error,
            "window_start": result.window_start,
            "window_end": result.window_end,
            "run_id": result.run_id,
        })
        row.results_json = json.dumps(existing)
        row.done += 1
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def _db_finish_batch(engine, batch_id: str) -> None:
    with Session(engine) as session:
        row = session.get(SQLUIBatchRun, batch_id)
        if row is None:
            return
        row.status = "finished"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def _db_cancel_batch(engine, batch_id: str) -> None:
    # The background thread checks status at each entity iteration (~1 ms SQLite read).
    # DB flag (vs threading.Event) survives machine restarts.
    with Session(engine) as session:
        row = session.get(SQLUIBatchRun, batch_id)
        if row is None:
            return
        row.status = "cancelled"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def _db_get_batch(engine, batch_id: str) -> SQLUIBatchRun | None:
    with Session(engine) as session:
        return session.get(SQLUIBatchRun, batch_id)


def _db_is_cancelled(engine, batch_id: str) -> bool:
    row = _db_get_batch(engine, batch_id)
    return row is not None and row.status == "cancelled"


# ── Background batch worker ───────────────────────────────────────────────────


def _run_one_ui_entity(
    *,
    batch_id: str,
    entity_id: str,
    force_window_end: datetime | None,
    engine,
    rate_limiter,
    connection_sem,
    http_client,
    source_categories: list[str] | None = None,
    startup_delay_seconds: float = 0.0,
) -> None:
    """Worker for one entity inside the parallel UI batch."""
    if startup_delay_seconds > 0:
        import time as _time
        _time.sleep(startup_delay_seconds)
    if _db_is_cancelled(engine, batch_id):
        logger.warning(
            "ui_batch_entity_skipped_cancelled",
            batch_id=batch_id,
            entity_id=entity_id,
        )
        _db_append_result(engine, batch_id, EntityRunStatus(
            entity_id=entity_id,
            entity_name=_get_entity_name(engine, entity_id),
            status="cancelled",
        ))
        return

    pipeline_config = load_pipeline_config_dict(resolve_config_path(None))
    if source_categories:
        pipeline_config["categories"] = source_categories
    state_dir = Path(".brief_pipeline_state")
    entity_name = _get_entity_name(engine, entity_id)

    logger.info(
        "ui_batch_entity_start",
        batch_id=batch_id,
        entity_id=entity_id,
        entity_name=entity_name,
    )
    t0 = time.perf_counter()
    try:
        force_window_start: datetime | None = None
        resolved_end = force_window_end
        if force_window_end is not None:
            with Session(engine) as _s:
                orch = _s.get(SQLEntityOrchestrationState, entity_id)
                last_end = orch.last_window_end if orch else None
            try:
                rd = build_report_dates_for_entity_run(
                    now=force_window_end,
                    last_window_end=last_end,
                    window_mode=WindowMode.DAILY,
                )
                force_window_start = rd.start
            except Exception:
                force_window_start = None
                resolved_end = None

        result = run_entity_incremental(
            entity_id=entity_id,
            pipeline_config=pipeline_config,
            state_dir=state_dir,
            force_window_start=force_window_start,
            force_window_end=resolved_end,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.exception(
            "ui_batch_entity_failed",
            batch_id=batch_id,
            entity_id=entity_id,
            entity_name=entity_name,
            elapsed_sec=round(elapsed, 2),
        )
        _db_append_result(engine, batch_id, EntityRunStatus(
            entity_id=entity_id,
            entity_name=entity_name,
            status="failed",
            error=str(exc),
        ))
        return

    entity_name = _get_entity_name(engine, entity_id)
    window_start = window_end = None
    if result.report_dates:
        window_start = result.report_dates.start.strftime("%Y-%m-%d %H:%M UTC")
        window_end = result.report_dates.end.strftime("%Y-%m-%d %H:%M UTC")

    _db_append_result(engine, batch_id, EntityRunStatus(
        entity_id=entity_id,
        entity_name=entity_name,
        status="succeeded" if result.success else "failed",
        error=result.error if not result.success else None,
        window_start=window_start,
        window_end=window_end,
        run_id=str(result.run_id) if result.run_id else None,
    ))
    elapsed = time.perf_counter() - t0
    n_bullets = len(result.new_bullets_novelty_ok) if result.new_bullets_novelty_ok else 0
    logger.info(
        "ui_batch_entity_done",
        batch_id=batch_id,
        entity_id=entity_id,
        entity_name=entity_name,
        success=result.success,
        run_id=str(result.run_id) if result.run_id else None,
        bullets_saved=n_bullets,
        elapsed_sec=round(elapsed, 2),
        error=result.error,
    )


def _ui_run_batch(
    *,
    batch_id: str,
    entity_ids: list[str],
    force_window_end: datetime | None,
    engine,
    executor,
    rate_limiter,
    connection_sem,
    http_client,
    source_categories: list[str] | None = None,
) -> None:
    """Submit all entities to the shared ThreadPoolExecutor in parallel with stagger.

    Each entity writes its result to the DB when it completes, so the polling
    route always reflects the current state even if entities finish out of order.
    When all futures are done the batch is marked finished.
    """
    import threading as _threading

    total = len(entity_ids)
    preview_ids = entity_ids if len(entity_ids) <= 24 else entity_ids[:24] + ["…"]
    logger.info(
        "ui_batch_workers_spawned",
        batch_id=batch_id,
        entity_count=total,
        entity_ids=preview_ids,
    )
    done_count = [0]
    lock = _threading.Lock()

    def _on_done(_future):
        with lock:
            done_count[0] += 1
            if done_count[0] == total:
                _db_finish_batch(engine, batch_id)
                logger.info("ui_batch_finished", batch_id=batch_id, entity_count=total)

    _ENTITY_STAGGER_SECONDS = 3.0
    for idx, entity_id in enumerate(entity_ids):
        delay = idx * _ENTITY_STAGGER_SECONDS if idx < settings.MAX_CONCURRENT_ENTITIES else 0.0
        future = executor.submit(
            _run_one_ui_entity,
            batch_id=batch_id,
            entity_id=entity_id,
            force_window_end=force_window_end,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
            source_categories=source_categories,
            startup_delay_seconds=delay,
        )
        future.add_done_callback(_on_done)


# ── Data helpers ──────────────────────────────────────────────────────────────


def _load_bullets_for_run(engine, run_id) -> list[dict]:
    """Load display-ready bullet dicts from SQLBulletRunLog for a given run_id.

    Returns a list of dicts in the same shape that _convert_bp used to produce
    from output_json, so all downstream renderers work unchanged. No output_json
    parsing needed.
    """
    with Session(engine) as session:
        rows = session.exec(
            select(SQLBulletRunLog)
            .where(SQLBulletRunLog.run_id == run_id)
            .order_by(SQLBulletRunLog.created_at)
        ).all()

    bullets = []
    for r in rows:
        citations = json.loads(r.citations_json or "[]")
        evaluator_details = json.loads(r.evaluator_details_json or "[]")
        claim_verdicts = json.loads(r.claim_verdicts_json or "[]")
        evidence_map = json.loads(r.evidence_map_json or "{}")
        grounding_citations = json.loads(r.grounding_citations_json or "[]")

        passed_block = None
        discarded_block = None
        if r.is_active:
            # For rewritten bullets use the post-rewrite search relevance check
            # (the last gate the bullet actually passed). For all others use the
            # initial relevance score from bullet generation.
            if r.search_verdict == "rewrite" and r.search_relevance_score is not None:
                display_score = r.search_relevance_score
                display_reason = r.search_relevance_reason or ""
            else:
                display_score = r.relevance_score
                display_reason = r.relevance_reason or ""
            passed_block = {
                "relevance_score": display_score,
                "relevance_reason": display_reason,
            }
        else:
            stage = r.discard_stage or "unknown"
            # Pick the reason field that matches the actual discard stage
            if stage == "relevance_score":
                discard_reason = r.relevance_reason or ""
            elif stage == "grounding":
                discard_reason = r.grounding_reason or ""
            elif stage in ("novelty_embedding", "novelty_embedding_relevance"):
                discard_reason = r.embedding_reason or ""
            elif stage == "novelty_search":
                discard_reason = r.search_reason or ""
            elif stage == "novelty_search_relevance":
                discard_reason = r.search_relevance_reason or ""
            else:
                discard_reason = ""
            # Pick the score that matches the actual discard stage
            if stage == "relevance_score":
                discard_score = r.relevance_score
            elif stage == "novelty_search_relevance":
                discard_score = r.search_relevance_score
            else:
                discard_score = None
            discarded_block = {
                "stage": stage,
                "reason": discard_reason,
                "score": discard_score,
                "citations": grounding_citations,
                "evaluator_details": evaluator_details,
                "claim_verdicts": claim_verdicts,
                "overall_verdict": r.search_overall_verdict or "",
                "evidence_map": evidence_map,
            }

        bullets.append({
            "id": str(r.id),
            "trace_id": r.trace_id,
            "text": r.text,
            "final_text": r.text,
            "original_text": r.original_text,
            "theme": r.theme,
            "citations": citations,
            "is_active": r.is_active,
            "not_fully_novel": r.not_fully_novel,
            "embedding_decision": r.embedding_decision,
            "search_action": r.search_verdict,
            "grounding_decision": r.grounding_decision,
            "passed": passed_block,
            "discarded": discarded_block,
        })
    return bullets


def _get_history_runs(engine, entity_id: str) -> list[SQLEntityPipelineRunLog]:
    """Return succeeded runs for entity ordered most-recent first."""
    with Session(engine) as session:
        rows = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).all()
        return list(rows)


def _get_distinct_entities(engine) -> list[tuple[str, str]]:
    """Return (entity_id, display_name) for all entities with succeeded runs."""
    with Session(engine) as session:
        rows = session.exec(
            select(SQLEntityOrchestrationState)
            .order_by(SQLEntityOrchestrationState.entity_id)
        ).all()
        # Filter to those with at least one succeeded run
        succeeded = set(session.exec(
            select(SQLEntityPipelineRunLog.entity_id)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .distinct()
        ).all())
    result = []
    for row in rows:
        if row.entity_id in succeeded:
            name = row.kg_name or row.entity_id
            result.append((row.entity_id, name))
    return result


# ── HTML rendering helpers (ported from build_brief_html_from_json_details.py) ──


def _nl_to_br(s: str) -> str:
    return html.escape(s or "", quote=False).replace("\n", "<br/>\n")


def _bullet_shows_partial_novelty(bp: dict) -> bool:
    """True for amber (partially novel): not_fully_novel flag OR search_action==rewrite."""
    if bp.get("not_fully_novel") is True:
        return True
    return str(bp.get("search_action") or "").strip().lower() == "rewrite"


def _render_one_citation_card(cit: dict, idx: int, *, inline: bool = False) -> str:
    cid = str(cit.get("id") or "").strip()
    headline = str(cit.get("headline") or "").strip()
    ctext = str(cit.get("text") or "").strip()
    src_name = str(cit.get("source_name") or "").strip()
    t = "span" if inline else "div"
    sb = ' style="display:block"' if inline else ""
    root_cls = "source-card" + (" source-card--inline" if inline else "")
    idx_esc = html.escape(str(idx))
    src_row = f'<{t} class="source-meta-row"{sb}>Source Name: {html.escape(src_name)}</{t}>' if src_name else ""
    meta = (
        f'<{t} class="source-meta-row source-meta-title"{sb}><strong>Source {idx_esc}</strong></{t}>'
        f'<{t} class="source-meta-row"{sb}>ID: {html.escape(cid) if cid else "—"}</{t}>'
        f"{src_row}"
    )
    return (
        f'<{t} class="{root_cls}"{sb}>'
        f'<{t} class="source-line source-line-stack"{sb}>{meta}</{t}>'
        f'<{t} class="hl-label"{sb}>Headline</{t}>'
        f'<{t} class="hl-body"{sb}>{_nl_to_br(headline or "—")}</{t}>'
        f'<{t} class="tx-label"{sb}>Text</{t}>'
        f'<{t} class="tx-body"{sb}>{_nl_to_br(ctext or "—")}</{t}>'
        f"</{t}>"
    )


def _render_inline_ref_badges(citations: list[dict], id_prefix: str) -> str:
    if not citations:
        return ""
    parts = []
    for i, cit in enumerate(citations, start=1):
        card = _render_one_citation_card(cit, i, inline=True)
        num_esc = html.escape(str(i))
        aria = html.escape(f"Reference {i}", quote=True)
        parts.append(
            '<span class="bullet-cite-inline-ref">'
            f'<span class="bullet-cite-ref-bracket" tabindex="0" role="button" aria-label="{aria}">'
            f'[{num_esc}]</span>'
            f'<span class="bullet-cite-ref-pop" role="region" aria-label="{aria}">{card}</span>'
            "</span>"
        )
    return f'<span class="bullet-ref-inline-cluster" role="group">{"".join(parts)}</span>'


def _render_bullet_prose(
    text: str,
    citations: list[dict],
    id_prefix: str,
    novelty_class: str,
    *,
    theme_html: str = "",
) -> str:
    plain = (text or "").strip() or "—"
    refs = _render_inline_ref_badges(citations, id_prefix)
    if not refs:
        inner = _nl_to_br(plain)
    else:
        dot = plain.rfind(".")
        if dot >= 0:
            inner = _nl_to_br(plain[: dot + 1]) + refs + _nl_to_br(plain[dot + 1 :])
        else:
            inner = _nl_to_br(plain) + refs
    if theme_html:
        inner += f'<span class="bullet-theme-suffix">{theme_html}</span>'
    classes = " ".join(c for c in ["bullet-text", "bullet-para", novelty_class] if c)
    return f'<div class="{classes}" role="paragraph">{inner}</div>'


def _render_citation_cards(citations: list[dict]) -> str:
    return "\n".join(_render_one_citation_card(c, i) for i, c in enumerate(citations, 1))


def _wrap_details_expander(inner: str, details_id: str) -> str:
    id_esc = html.escape(details_id, quote=True)
    return (
        f'<details class="bullet-refs" id="{id_esc}">'
        f'<summary><span class="ref-trigger">Details</span></summary>'
        f'<div class="refs-inner">{inner}</div>'
        "</details>"
    )


# ── BP record conversion ──────────────────────────────────────────────────────


def _get_discard_stage(bp: dict) -> str:
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


_DISCARD_STAGE_LABELS: dict[str, str] = {
    "relevance_score": "Relevance",
    "grounding": "Grounding",
    "novelty_embedding": "Novelty (embedding)",
    "novelty_embedding_relevance": "Relevance after embedding rewrite",
    "novelty_search": "Novelty (search)",
    "novelty_search_relevance": "Relevance after search rewrite",
    "unknown": "Unknown stage",
}

_DISCARD_STAGE_ORDER = [
    "relevance_score", "grounding", "novelty_embedding",
    "novelty_embedding_relevance", "novelty_search", "novelty_search_relevance", "unknown",
]


def _build_doc_index(source_refs: dict) -> dict:
    """Build a lookup index from source_references for resolving citation IDs.

    Citation IDs in bullet points use format 'CQS:{document_id}-{chunk}', but
    source_references keys are 'CQS:REF0', 'CQS:REF1' etc. A direct lookup
    always fails. This index maps '{document_id}-{chunk_id}' (and '{document_id}'
    as fallback) to the metadata, matching the same logic used in report.py.
    """
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
            exact_key = f"{doc_id}-{chunk_id}"
            if exact_key not in index:
                index[exact_key] = entry
        if doc_id not in index:
            index[doc_id] = entry
    return index


def _resolve_citation(cit_id: str, doc_index: dict) -> dict:
    tail = cit_id.split(":", 1)[-1]
    doc_id_only = tail.rsplit("-", 1)[0]
    meta = doc_index.get(tail) or doc_index.get(doc_id_only) or {}
    return {
        "id": cit_id,
        "headline": meta.get("headline") or "",
        "text": meta.get("text") or "",
        "source_name": meta.get("source_name") or "",
        "date": meta.get("date") or "",
    }


def _convert_bp(bp: dict, source_refs: dict) -> dict:
    """Convert a BulletPointRecord dict + source_refs into a display-ready dict."""
    doc_index = _build_doc_index(source_refs)
    citations = [
        _resolve_citation(str(ref_id), doc_index)
        for ref_id in (bp.get("citations") or [])
    ]

    gen = bp.get("generation") or {}
    ne = bp.get("novelty_embedding") or {}
    ns_block = bp.get("novelty_search") or {}
    s = ns_block.get("search") or {}

    emb_decision = (ne.get("judgment") or {}).get("decision")
    search_action = s.get("verdict")
    is_active = bp.get("is_active", True)
    overall_verdict = s.get("overall_verdict")
    not_fully_novel = bool(is_active and overall_verdict in ("novel_with_context", "partial_update_with_context", "multi_partial_update"))

    ne_rewrite = (ne.get("rewrite") or {}).get("text_after")
    search_rewrite = s.get("rewritten_text")
    final_text = search_rewrite or ne_rewrite or bp.get("text", "")
    original_text = gen.get("original_text", "")

    passed_block: dict | None = None
    discarded_block: dict | None = None
    if is_active:
        rs = bp.get("relevance_scoring") or {}
        passed_block = {"relevance_score": rs.get("score"), "relevance_reason": rs.get("reason", "")}
    else:
        stage = _get_discard_stage(bp)
        ne_j = ne.get("judgment") or {}
        search_details = s.get("details") or {}
        discarded_block = {
            "stage": stage,
            "reason": (
                (bp.get("relevance_scoring") or {}).get("reason", "") if stage == "relevance_score"
                else ((bp.get("entity_grounding") or {}).get("check") or {}).get("reason", "") if stage == "grounding"
                else ne_j.get("reason", "") if stage == "novelty_embedding"
                else s.get("reason", "") if stage == "novelty_search"
                else (ns_block.get("relevance_check") or {}).get("reasoning", "") if stage == "novelty_search_relevance"
                else ""
            ),
            "score": (bp.get("relevance_scoring") or {}).get("score") if stage == "relevance_score" else None,
            "citations": [str(c) for c in (bp.get("citations") or [])] if stage == "grounding" else [],
            "evaluator_details": ne_j.get("evaluator_details") or [] if stage == "novelty_embedding" else [],
            "claim_verdicts": search_details.get("claim_verdicts") or [] if stage == "novelty_search" else [],
            "overall_verdict": s.get("overall_verdict", "") if stage == "novelty_search" else "",
            # evidence_map: simple_id → {headline, date, text} — needed to resolve
            # evidence_ids referenced inside each claim_verdict
            "evidence_map": search_details.get("evidence_map") or {} if stage == "novelty_search" else {},
        }

    return {
        "trace_id": bp.get("trace_id", ""),
        "text": bp.get("text", ""),
        "citations": citations,
        "embedding_decision": emb_decision,
        "search_action": search_action,
        "not_fully_novel": not_fully_novel,
        "theme": bp.get("theme", ""),
        "original_text": original_text,
        "final_text": final_text,
        "is_active": is_active,
        "passed": passed_block,
        "discarded": discarded_block,
    }


# ── Bullet block renderers ────────────────────────────────────────────────────


def _render_active_bullet(b: dict, idx: int, bid: str, include_details: bool) -> str:
    theme = str(b.get("theme") or "").strip()
    theme_html = f'<span class="theme-pill">{html.escape(theme)}</span>' if theme else ""
    novelty_class = "bullet-not-fully-novel" if _bullet_shows_partial_novelty(b) else "bullet-fully-novel"
    published = (b.get("final_text") or b.get("text") or "").strip()
    citations = b.get("citations") or []
    prose = _render_bullet_prose(published, citations, f"{bid}-cite", novelty_class, theme_html=theme_html)

    parts = [f'<li value="{idx}">', prose]

    if include_details:
        original = str(b.get("original_text") or "").strip()
        final = str(b.get("final_text") or "").strip()
        detail_parts: list[str] = []
        passed = b.get("passed") or {}
        rs = passed.get("relevance_score")
        rr = str(passed.get("relevance_reason") or "").strip()
        if isinstance(rs, (int, float)):
            s = int(rs)
            pips = "".join(
                f'<span style="width:10px;height:10px;border-radius:50%;background:{"#166534" if i <= s else "#e5e7eb"};display:inline-block"></span>'
                for i in range(1, 6)
            )
            score_html = (
                f'<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem">'
                f'<span style="font-size:.78rem;font-weight:700;color:#166534">{s}/5</span>'
                f'<span style="display:inline-flex;gap:3px;align-items:center">{pips}</span>'
                f'</div>'
            )
        else:
            score_html = ""
        detail_parts.append(
            f'<div class="detail-panel">'
            f'<div class="detail-label">Relevance</div>'
            f'{score_html}'
            f'<div class="detail-reason">{_nl_to_br(rr or "—")}</div>'
            f'</div>'
        )
        if original and final and original != final:
            detail_parts.append(
                '<div class="detail-panel"><div class="detail-label">Original draft</div>'
                f'<div class="tx-body">{_nl_to_br(original)}</div></div>'
            )
        if citations:
            detail_parts.append(
                '<hr style="border:none;border-top:3px solid var(--border);margin:.5rem 0"/>'
                '<div class="detail-panel"><div class="detail-label">Sources</div>'
                f'{_render_citation_cards(citations)}</div>'
            )
        inner = "".join(detail_parts)
        if inner.strip():
            parts.append(_wrap_details_expander(inner, f"{bid}-det"))

    parts.append("</li>")
    return "".join(parts)


_NOVELTY_VERDICT_COLORS: dict[str, str] = {
    "novel":               ("color:#166534", "background:#dcfce7", "border:1px solid #86efac"),
    "novel_with_context":          ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
    "novel_noisy":                 ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
    "partial_update":              ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
    "partial_update_with_context": ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
    "multi_partial_update":        ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
    "discard_not_new":     ("color:#991b1b", "background:#fee2e2", "border:1px solid #fca5a5"),
    "discard_unsupported": ("color:#991b1b", "background:#fee2e2", "border:1px solid #fca5a5"),
    "old":                 ("color:#6b7280", "background:#f3f4f6", "border:1px solid #d1d5db"),
    "discard":             ("color:#991b1b", "background:#fee2e2", "border:1px solid #fca5a5"),
    "keep":                ("color:#166534", "background:#dcfce7", "border:1px solid #86efac"),
    "rewrite":             ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
}

_EVALUATOR_DECISION_COLORS: dict[str, tuple] = {
    "discard": ("color:#991b1b", "background:#fee2e2", "border:1px solid #fca5a5"),
    "keep":    ("color:#166534", "background:#dcfce7", "border:1px solid #86efac"),
    "rewrite": ("color:#92400e", "background:#fef3c7", "border:1px solid #fcd34d"),
}


def _verdict_badge(verdict: str) -> str:
    style_parts = _NOVELTY_VERDICT_COLORS.get(verdict.lower() if verdict else "", ("color:#374151", "background:#f3f4f6", "border:1px solid #d1d5db"))
    style = ";".join(style_parts)
    label = verdict.replace("_", " ") if verdict else "—"
    return f'<span style="display:inline-block;font-size:.72rem;font-weight:700;padding:.15rem .5rem;border-radius:5px;{style}">{html.escape(label)}</span>'


def _render_discarded_detail_body(b: dict) -> str:
    d = b.get("discarded") or {}
    stage = str(d.get("stage") or "unknown")
    reason = str(d.get("reason") or "").strip()
    parts = ['<div class="detail-panel">']

    # ── Relevance scoring ─────────────────────────────────────────────────────
    if stage == "relevance_score":
        score = d.get("score")
        header = '<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem">'
        if isinstance(score, (int, float)):
            s = int(score)
            pips = "".join(
                f'<span style="width:10px;height:10px;border-radius:50%;background:{"#dc2626" if i <= s else "#e5e7eb"};display:inline-block"></span>'
                for i in range(1, 6)
            )
            header += (
                f'<span style="font-size:.78rem;font-weight:700;color:#dc2626">{s}/5</span>'
                f'<span style="display:inline-flex;gap:3px;align-items:center">{pips}</span>'
            )
        header += '</div>'
        parts.append(header)
        if reason:
            parts.append(f'<div class="detail-reason">{_nl_to_br(reason)}</div>')

    # ── Entity grounding ──────────────────────────────────────────────────────
    elif stage == "grounding":
        if reason:
            parts.append(f'<div class="detail-reason">{_nl_to_br(reason)}</div>')

    # ── Novelty embedding / embedding relevance ───────────────────────────────
    elif stage in ("novelty_embedding", "novelty_embedding_relevance"):
        if reason:
            parts.append(f'<div class="detail-reason" style="margin-bottom:.75rem">{_nl_to_br(reason)}</div>')

        # Text diff for rewrite+relevance fail
        original = str(b.get("original_text") or "").strip()
        final = str(b.get("final_text") or "").strip()
        if original and final and original != final:
            parts.append(
                '<div class="text-compare" style="margin-bottom:.75rem">'
                f'<div class="text-compare-col"><h5>Original</h5><div class="detail-body">{_nl_to_br(original)}</div></div>'
                f'<div class="text-compare-col"><h5>Rewritten</h5><div class="detail-body">{_nl_to_br(final)}</div></div>'
                '</div>'
            )

        for ev in (d.get("evaluator_details") or []):
            if not isinstance(ev, dict):
                continue
            ename = html.escape(str(ev.get("evaluator_name") or "evaluator"))
            decision = str(ev.get("decision") or "")
            ev_reason = str(ev.get("reason") or "").strip()
            ev_style = ";".join(_EVALUATOR_DECISION_COLORS.get(decision, ("color:#374151", "background:#f3f4f6", "border:1px solid #d1d5db")))
            parts.append(
                f'<div class="evaluator-block">'
                f'<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem">'
                f'<span style="font-size:.78rem;font-weight:700;color:#475569">{ename}</span>'
                f'<span style="font-size:.72rem;font-weight:700;padding:.1rem .4rem;border-radius:4px;{ev_style}">{html.escape(decision)}</span>'
                f'</div>'
            )
            if ev_reason:
                parts.append(f'<div class="detail-reason" style="margin-bottom:.4rem">{_nl_to_br(ev_reason)}</div>')
            rbs = [r for r in (ev.get("retrieved_bullets") or []) if isinstance(r, dict)]
            if rbs:
                parts.append('<div style="display:flex;flex-direction:column;gap:.35rem">')
                for rb in rbs:
                    rb_text = html.escape(str(rb.get("text") or ""))
                    rb_score = rb.get("score")
                    rb_date = html.escape(str(rb.get("date") or ""))
                    score_s = f"{float(rb_score):.2f}" if isinstance(rb_score, (int, float)) else "—"
                    parts.append(
                        f'<div class="retrieved-bullet-card">'
                        f'<div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.25rem">'
                        f'<span style="font-size:.72rem;font-weight:700;color:#1e40af;background:#dbeafe;padding:.1rem .35rem;border-radius:4px">sim {score_s}</span>'
                        f'<span style="font-size:.75rem;color:var(--muted)">{rb_date}</span>'
                        f'</div>'
                        f'<div class="tx-body">{_nl_to_br(rb_text)}</div>'
                        f'</div>'
                    )
                parts.append('</div>')
            parts.append('</div>')

    # ── Novelty search / search relevance ─────────────────────────────────────
    elif stage in ("novelty_search", "novelty_search_relevance"):
        ov = str(d.get("overall_verdict") or "").strip()
        evidence_map: dict = d.get("evidence_map") or {}
        header = '<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem">'
        if ov:
            header += _verdict_badge(ov)
        # For novelty_search_relevance, show the score with pip dots (same style as
        # initial relevance_score stage so the reader knows why it was cut)
        if stage == "novelty_search_relevance":
            score = d.get("score")
            if isinstance(score, (int, float)):
                s = int(score)
                pips = "".join(
                    f'<span style="width:10px;height:10px;border-radius:50%;background:{"#dc2626" if i <= s else "#e5e7eb"};display:inline-block"></span>'
                    for i in range(1, 6)
                )
                header += (
                    f'<span style="font-size:.78rem;font-weight:700;color:#dc2626">{s}/5</span>'
                    f'<span style="display:inline-flex;gap:3px;align-items:center">{pips}</span>'
                )
        if reason:
            header += f'<span style="font-size:.85rem;color:#374151">{_nl_to_br(reason)}</span>'
        header += '</div>'
        parts.append(header)

        # Text diff for search rewrite+relevance fail
        original = str(b.get("original_text") or "").strip()
        final = str(b.get("final_text") or "").strip()
        if original and final and original != final:
            parts.append(
                '<div class="text-compare" style="margin-bottom:.75rem">'
                f'<div class="text-compare-col"><h5>Original</h5><div class="detail-body">{_nl_to_br(original)}</div></div>'
                f'<div class="text-compare-col"><h5>Rewritten</h5><div class="detail-body">{_nl_to_br(final)}</div></div>'
                '</div>'
            )

        claims = [c for c in (d.get("claim_verdicts") or []) if isinstance(c, dict)]
        if claims:
            parts.append('<div style="display:flex;flex-direction:column;gap:.5rem">')
            for cv in claims:
                idx = cv.get("claim_index")
                ctext = str(cv.get("claim_text") or "").strip()
                nov = str(cv.get("novelty") or "")
                rsn = str(cv.get("reasoning") or "").strip()
                idx_s = f"#{html.escape(str(idx))}" if idx is not None else ""
                # evidence_ids are simple_id strings (e.g. "D18-C2"); resolve via evidence_map
                evidence_ids: list[str] = [str(e) for e in (cv.get("evidence_ids") or []) if e]
                evidence_items = [(eid, evidence_map.get(eid) or {}) for eid in evidence_ids]
                parts.append(
                    f'<div class="claim-block">'
                    f'<div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.35rem">'
                    f'<span style="font-size:.75rem;font-weight:700;color:var(--muted)">{idx_s}</span>'
                    + _verdict_badge(nov) +
                    f'</div>'
                    f'<div class="detail-body" style="margin-bottom:.3rem">{_nl_to_br(ctext or "—")}</div>'
                )
                if rsn:
                    parts.append(f'<div style="font-size:.8rem;color:#475569;font-style:italic;margin-bottom:.4rem">{_nl_to_br(rsn)}</div>')
                if evidence_items:
                    parts.append('<div style="display:flex;flex-direction:column;gap:.3rem">')
                    for eid, ev in evidence_items:
                        hl = html.escape(str(ev.get("headline") or "—"))
                        dt = html.escape(str(ev.get("date") or ""))
                        sid = html.escape(eid)
                        tx = str(ev.get("text") or "").strip()
                        parts.append(
                            f'<div class="evidence-card">'
                            f'<div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.2rem">'
                            f'<span style="font-size:.72rem;font-family:monospace;color:var(--muted)">{sid}</span>'
                            f'<span style="font-size:.72rem;color:var(--muted)">{dt}</span>'
                            f'</div>'
                            f'<div class="hl-label">Headline</div>'
                            f'<div class="hl-body">{_nl_to_br(hl)}</div>'
                            + (
                                f'<div class="tx-label">Text</div>'
                                f'<div class="tx-body">{_nl_to_br(tx)}</div>'
                                if tx else ""
                            ) +
                            f'</div>'
                        )
                    parts.append('</div>')
                parts.append('</div>')
            parts.append('</div>')

    # ── Fallback (unknown stage) ───────────────────────────────────────────────
    else:
        if reason:
            parts.append(f'<div class="detail-reason">{_nl_to_br(reason)}</div>')

    parts.append("</div>")
    return "".join(parts)


def _render_discarded_section(discarded: list[dict], section_id: str) -> str:
    if not discarded:
        return ""
    buckets: dict[str, list[dict]] = {}
    for b in discarded:
        # _convert_bp already computed the stage and stored it in b["discarded"]["stage"].
        # Calling _get_discard_stage() here would always return "unknown" because the
        # raw pipeline fields (relevance_scoring, entity_grounding, etc.) are gone.
        stage = (b.get("discarded") or {}).get("stage") or _get_discard_stage(b)
        buckets.setdefault(stage, []).append(b)

    total = len(discarded)
    blocks: list[str] = []
    for stage in _DISCARD_STAGE_ORDER:
        items = buckets.get(stage)
        if not items:
            continue
        title = html.escape(_DISCARD_STAGE_LABELS.get(stage, stage))
        lis: list[str] = []
        for i_idx, b in enumerate(items, 1):
            bid = f"{section_id}-{stage[:4]}-{i_idx}"
            original = str(b.get("original_text") or b.get("text") or "").strip()
            citations = b.get("citations") or []
            prose = _render_bullet_prose(original, citations, f"{bid}-cite", "bullet-text-discarded")
            detail_body = _render_discarded_detail_body(b)
            nested = _wrap_details_expander(detail_body, f"{bid}-det") if detail_body.strip() else ""
            lis.append(f"<li>{prose}{nested}</li>")

        blocks.append(
            f'<div class="discard-category">'
            f'<h4 class="discard-cat-title">{title} <span class="discard-cat-count">({len(items)})</span></h4>'
            f'<ul class="discard-ul">{"".join(lis)}</ul>'
            f"</div>"
        )

    id_esc = html.escape(section_id, quote=True)
    return (
        f'<details class="run-discarded" id="{id_esc}">'
        f'<summary><span class="discard-trigger">Discarded</span>'
        f'<span class="discard-count">({total})</span></summary>'
        f'<div class="discard-inner">{"".join(blocks)}</div>'
        f"</details>"
    )


def _render_run_bullets_html(
    bullets: list[dict],
    run_key: str,
    *,
    include_discarded: bool,
    include_details: bool,
) -> str:
    """Render bullets already in display-ready format (from _load_bullets_for_run)."""
    active = [b for b in bullets if b.get("is_active", True)]
    discarded = [b for b in bullets if not b.get("is_active", True)]

    # Sort active: fully-novel first, then amber
    active_sorted = sorted(active, key=lambda b: (
        str(b.get("theme") or "").lower() or "￿",
        1 if _bullet_shows_partial_novelty(b) else 0,
    ))

    parts: list[str] = []
    if active_sorted:
        parts.append('<ol class="bullets">')
        for idx, b in enumerate(active_sorted, 1):
            bid = f"{run_key}-b{idx}"
            parts.append(_render_active_bullet(b, idx, bid, include_details))
        parts.append("</ol>")
    else:
        parts.append('<p class="run-empty-day">No passed bullets in this window.</p>')

    if include_discarded and discarded:
        parts.append(_render_discarded_section(discarded, f"{run_key}-disc"))

    return "".join(parts)


def _render_entity_history_html(
    runs: list[SQLEntityPipelineRunLog],
    engine,
    *,
    include_discarded: bool,
    include_details: bool,
) -> str:
    """Group runs by day → by run-end HH:MM and render."""
    if not runs:
        return '<p class="run-empty-day">No completed runs found for this entity.</p>'

    # Group by calendar date of report_window_start
    from collections import defaultdict
    days: dict[str, list[SQLEntityPipelineRunLog]] = defaultdict(list)
    for log in runs:
        day = log.report_window_start.date().isoformat() if log.report_window_start else "unknown"
        days[day].append(log)

    parts: list[str] = []
    for day in sorted(days.keys(), reverse=True):
        day_runs = sorted(
            days[day],
            key=lambda r: r.process_completed_at_utc or datetime.min,
            reverse=True,
        )
        parts.append(f'<div class="run-day">{html.escape(day)}</div>')
        for run_idx, log in enumerate(day_runs, 1):
            end_label = ""
            if log.process_completed_at_utc:
                end_label = f" — run ended {log.process_completed_at_utc.strftime('%H:%M')} UTC"
            parts.append(f'<section class="run-block">')
            if end_label:
                parts.append(f'<div class="run-time-label">{html.escape(end_label.strip())}</div>')
            run_key = f"hist-{log.run_id}-{run_idx}"
            bullets = _load_bullets_for_run(engine, log.run_id)
            parts.append(_render_run_bullets_html(
                bullets, run_key,
                include_discarded=include_discarded,
                include_details=include_details,
            ))
            parts.append("</section>")

    return "".join(parts)


# ── Page routes ───────────────────────────────────────────────────────────────


@router.get("/run", response_class=HTMLResponse)
async def ui_run_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "ui/run.html",
        {
            "universe_names": sorted(_UNIVERSES.keys()),
            "preset_names": list(settings.ENTITY_LISTS.keys()),
        },
    )


@router.get("/history", response_class=HTMLResponse)
async def ui_history_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()
    entities = _get_distinct_entities(engine)
    return templates.TemplateResponse(
        request, "ui/history.html",
        {"entities": entities, "page": "history"},
    )


@router.get("/history-details", response_class=HTMLResponse)
async def ui_history_details_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()
    entities = _get_distinct_entities(engine)
    return templates.TemplateResponse(
        request, "ui/history.html",
        {"entities": entities, "page": "history-details"},
    )


# ── HTMX partial routes ───────────────────────────────────────────────────────


@router.post("/batch/run", response_class=HTMLResponse)
async def ui_batch_run(
    request: Request,
    entity_ids_raw: str = Form(default=""),
    preset_name: str = Form(default=""),
    universe_name: str = Form(default=""),
    window_end_str: str = Form(default=""),
    source_categories: list[str] = Form(default=[]),
) -> HTMLResponse:
    templates = request.app.state.templates

    # Resolve entity ID list: universe > preset > raw IDs
    ids: list[str] = []
    if universe_name and universe_name in _UNIVERSES:
        ids = list(_UNIVERSES[universe_name])
    elif preset_name and preset_name in settings.ENTITY_LISTS:
        ids = settings.ENTITY_LISTS[preset_name]
    elif entity_ids_raw.strip():
        import re
        ids = [x.strip() for x in re.split(r"[\n,]+", entity_ids_raw) if x.strip()]

    if not ids:
        return HTMLResponse('<p style="color:#dc2626">No entity IDs provided.</p>')

    force_window_end: datetime | None = None
    if window_end_str.strip():
        try:
            force_window_end = datetime.fromisoformat(window_end_str).replace(tzinfo=timezone.utc)
        except ValueError:
            return HTMLResponse('<p style="color:#dc2626">Invalid end date format. Use YYYY-MM-DDTHH:MM</p>')

    batch_id = str(uuid.uuid4())
    engine = get_engine()
    _db_create_batch(engine, batch_id, ids)

    _ui_run_batch(
        batch_id=batch_id,
        entity_ids=ids,
        force_window_end=force_window_end,
        engine=engine,
        executor=get_entity_executor(request),
        rate_limiter=get_rate_limiter(request),
        connection_sem=get_connection_sem(request),
        http_client=get_http_client(request),
        source_categories=source_categories or [],
    )
    logger.info(
        "ui_batch_run_queued",
        batch_id=batch_id,
        entity_count=len(ids),
        source_categories=source_categories or None,
    )

    return templates.TemplateResponse(
        request, "ui/partials/run_progress.html",
        {"batch_id": batch_id, "total": len(ids), "done": 0},
    )


@router.post("/batch/stop", response_class=HTMLResponse)
async def ui_batch_stop(request: Request, batch_id: str = Form(default="")) -> HTMLResponse:
    engine = get_engine()
    _db_cancel_batch(engine, batch_id)
    return HTMLResponse('<p style="color:#92400e;font-size:0.9rem">Stop requested — waiting for current entity to finish.</p>')


@router.get("/partials/run-status", response_class=HTMLResponse)
async def ui_run_status(request: Request, batch_id: str = "") -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()
    batch = _db_get_batch(engine, batch_id)

    if batch is None:
        return HTMLResponse('<p style="color:#dc2626">Batch not found.</p>')

    if batch.status == "running":
        return templates.TemplateResponse(
            request, "ui/partials/run_progress.html",
            {"batch_id": batch_id, "total": batch.total, "done": batch.done},
        )

    # finished or cancelled — read results and render
    engine = get_engine()
    statuses: list[EntityRunStatus] = []
    for r in json.loads(batch.results_json):
        statuses.append(EntityRunStatus(
            entity_id=r["entity_id"],
            entity_name=r["entity_name"],
            status=r["status"],
            error=r.get("error"),
            window_start=r.get("window_start"),
            window_end=r.get("window_end"),
            run_id=r.get("run_id"),
        ))

    results_html = _render_batch_results(statuses, engine)
    return templates.TemplateResponse(
        request, "ui/partials/run_result.html",
        {"results_html": results_html},
    )


def _render_batch_results(statuses: list[EntityRunStatus], engine) -> str:
    parts: list[str] = []
    for s_idx, s in enumerate(statuses, 1):
        name_esc = html.escape(s.entity_name or s.entity_id)
        eid_esc = html.escape(s.entity_id)
        parts.append(
            f'<article class="entity" data-entity="{eid_esc}">'
            f'<header class="entity-header"><h2>{name_esc}</h2>'
            f'<span class="entity-id">{eid_esc}</span>'
        )
        if s.window_start:
            parts.append(
                f'<span class="entity-id" style="margin-left:1rem">'
                f'{html.escape(s.window_start)} → {html.escape(s.window_end or "")}</span>'
            )
        parts.append("</header>")

        if s.status in ("failed", "cancelled"):
            err = html.escape(s.error or s.status)
            parts.append(f'<div class="run-block"><p style="color:#dc2626">{err}</p></div>')
        elif s.status == "skipped":
            parts.append('<div class="run-block"><p class="run-empty-day">Skipped.</p></div>')
        else:
            run_key = f"res-e{s_idx}"
            bullets = _load_bullets_for_run(engine, uuid.UUID(s.run_id)) if s.run_id else []
            bullets_html = _render_run_bullets_html(
                bullets, run_key,
                include_discarded=True,
                include_details=False,
            )
            parts.append(f'<section class="run-block">{bullets_html}</section>')

        parts.append("</article>")
    return "".join(parts)


@router.get("/partials/history", response_class=HTMLResponse)
async def ui_history_partial(request: Request, entity_id: str = "") -> HTMLResponse:
    templates = request.app.state.templates
    if not entity_id:
        return HTMLResponse("")
    engine = get_engine()
    runs = _get_history_runs(engine, entity_id)
    history_html = _render_entity_history_html(runs, engine, include_discarded=False, include_details=False)
    return templates.TemplateResponse(
        request, "ui/partials/history_content.html",
        {"history_html": history_html},
    )


@router.get("/partials/history-details", response_class=HTMLResponse)
async def ui_history_details_partial(request: Request, entity_id: str = "") -> HTMLResponse:
    templates = request.app.state.templates
    if not entity_id:
        return HTMLResponse("")
    engine = get_engine()
    runs = _get_history_runs(engine, entity_id)
    history_html = _render_entity_history_html(runs, engine, include_discarded=True, include_details=True)
    return templates.TemplateResponse(
        request, "ui/partials/history_content.html",
        {"history_html": history_html},
    )


# ── Admin routes ──────────────────────────────────────────────────────────────


@router.get("/admin", response_class=HTMLResponse)
async def ui_admin_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()
    entities = _get_all_entities(engine)
    return templates.TemplateResponse(
        request, "ui/admin.html",
        {"entities": entities},
    )


@router.post("/admin/reset-db", response_class=HTMLResponse)
async def ui_admin_reset_db(request: Request) -> HTMLResponse:
    from bigdata_briefs.orchestration.db import ensure_orchestration_schema
    engine = get_engine()
    from sqlmodel import SQLModel
    SQLModel.metadata.drop_all(engine)
    ensure_orchestration_schema(engine)
    return HTMLResponse('<p class="admin-ok">Database reset. All tables recreated empty.</p>')


@router.post("/admin/delete-entity", response_class=HTMLResponse)
async def ui_admin_delete_entity(
    request: Request,
    entity_id: str = Form(default=""),
) -> HTMLResponse:
    if not entity_id.strip():
        return HTMLResponse('<p class="admin-err">No entity ID provided.</p>')

    from bigdata_briefs.novelty.sql_models import (
        SQLBulletPointEmbedding,
        SQLChunkTextHash,
        SQLGeneratedBulletPoint,
    )
    from bigdata_briefs.novelty.sql_pipeline_checkpoint import SQLBulletPipelineCheckpoint

    engine = get_engine()
    eid = entity_id.strip()

    with Session(engine) as session:
        for model in (
            SQLEntityPipelineRunLog,
            SQLEntityOrchestrationState,
            SQLBulletPointEmbedding,
            SQLGeneratedBulletPoint,
            SQLChunkTextHash,
            SQLBulletPipelineCheckpoint,
        ):
            session.exec(sa_delete(model).where(model.entity_id == eid))
        session.commit()

    return HTMLResponse(f'<p class="admin-ok">All data for <code>{html.escape(eid)}</code> deleted.</p>')


def _get_all_entities(engine) -> list[tuple[str, str]]:
    """Return (entity_id, display_name) for all known entities."""
    with Session(engine) as session:
        rows = session.exec(
            select(SQLEntityOrchestrationState)
            .order_by(SQLEntityOrchestrationState.entity_id)
        ).all()
    return [(r.entity_id, r.kg_name or r.entity_id) for r in rows]


def _get_universe_entities_with_names() -> list[tuple[str, str]]:
    """Return (entity_id, name) for all unique entities across all universes.

    Names come from the CSV files (which have both id and name columns).
    Sorted by name, deduplicated by entity_id.
    """
    import csv as _csv
    seen: dict[str, str] = {}
    for csv_path in sorted(_UNIVERSES_DIR.glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                eid = (row.get("id") or "").strip()
                name = (row.get("name") or eid).strip()
                if eid and eid not in seen:
                    seen[eid] = name
    return sorted(seen.items(), key=lambda x: x[1])


# ── Scan routes ───────────────────────────────────────────────────────────────


@router.get("/scan", response_class=HTMLResponse)
async def ui_scan_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    entities = _get_universe_entities_with_names()
    entities_by_universe = {name: list(ids) for name, ids in _UNIVERSES.items()}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request, "ui/scan.html",
        {
            "entities": entities,
            "universe_names": sorted(_UNIVERSES.keys()),
            "entities_by_universe_json": json.dumps(entities_by_universe),
            "today": today,
        },
    )


def _resolve_entity_name(engine, entity_id: str) -> str:
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        if orch and orch.kg_name:
            return orch.kg_name
    return dict(_get_universe_entities_with_names()).get(entity_id, entity_id)


@router.post("/scan/run", response_class=HTMLResponse)
async def ui_scan_run(
    request: Request,
    entity_id: str = Form(default=""),
    universe_name: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    source_categories: list[str] = Form(default=[]),
) -> HTMLResponse:
    templates = request.app.state.templates

    if not start_date.strip():
        return HTMLResponse('<p style="color:#dc2626">Start date is required.</p>')
    if not entity_id.strip() and not universe_name.strip():
        return HTMLResponse('<p style="color:#dc2626">Select an entity or a universe.</p>')

    try:
        requested_start = datetime.strptime(start_date.strip(), "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=timezone.utc
        )
    except ValueError:
        return HTMLResponse('<p style="color:#dc2626">Invalid start date.</p>')

    if end_date.strip():
        try:
            end = datetime.strptime(end_date.strip(), "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            return HTMLResponse('<p style="color:#dc2626">Invalid end date.</p>')
    else:
        end = datetime.now(timezone.utc)

    engine = get_engine()
    executor = get_entity_executor(request)
    rate_limiter = get_rate_limiter(request)
    connection_sem = get_connection_sem(request)
    http_client = get_http_client(request)

    # ── Universe scan: one scan per entity, show list of progress bars ────────
    if universe_name.strip():
        entity_ids = _UNIVERSES.get(universe_name.strip())
        if not entity_ids:
            return HTMLResponse(f'<p style="color:#dc2626">Universe "{universe_name}" not found.</p>')

        all_entity_names = dict(_get_universe_entities_with_names())
        scan_items: list[dict] = []
        for eid in entity_ids:
            effective_start = resolve_scan_start(engine, eid, requested_start, end)
            windows = build_scan_windows(effective_start, end)
            if not windows:
                continue
            entity_name = _resolve_entity_name(engine, eid)
            scan_id = str(uuid.uuid4())
            db_create_scan(engine, scan_id, eid, entity_name, len(windows))
            executor.submit(
                run_scan_worker,
                scan_id=scan_id,
                entity_id=eid,
                windows=windows,
                engine=engine,
                rate_limiter=rate_limiter,
                connection_sem=connection_sem,
                http_client=http_client,
                source_categories=source_categories or None,
            )
            scan_items.append({
                "scan_id": scan_id,
                "entity_name": entity_name,
                "windows_total": len(windows),
                "effective_start": effective_start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d %H:%M UTC"),
            })

        if not scan_items:
            return HTMLResponse('<p style="color:#92400e">All entities in this universe are already up to date.</p>')

        return templates.TemplateResponse(
            request, "ui/partials/scan_universe_progress.html",
            {"scans": scan_items},
        )

    # ── Single entity scan ────────────────────────────────────────────────────
    eid = entity_id.strip()
    effective_start = resolve_scan_start(engine, eid, requested_start, end)
    windows = build_scan_windows(effective_start, end)

    if not windows:
        return HTMLResponse('<p style="color:#92400e">No windows to process — entity is already up to date for this range.</p>')

    entity_name = _resolve_entity_name(engine, eid)
    scan_id = str(uuid.uuid4())
    db_create_scan(engine, scan_id, eid, entity_name, len(windows))

    executor.submit(
        run_scan_worker,
        scan_id=scan_id,
        entity_id=eid,
        windows=windows,
        engine=engine,
        rate_limiter=rate_limiter,
        connection_sem=connection_sem,
        http_client=http_client,
        source_categories=source_categories or None,
    )

    return templates.TemplateResponse(
        request, "ui/partials/scan_progress.html",
        {
            "scan_id": scan_id,
            "entity_name": entity_name,
            "windows_total": len(windows),
            "windows_done": 0,
            "effective_start": effective_start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


@router.post("/scan/stop", response_class=HTMLResponse)
async def ui_scan_stop(request: Request, scan_id: str = Form(default="")) -> HTMLResponse:
    engine = get_engine()
    db_cancel_scan(engine, scan_id)
    return HTMLResponse('<p style="color:#92400e;font-size:.9rem">Stop requested — waiting for current day to finish.</p>')


@router.get("/partials/scan-status", response_class=HTMLResponse)
async def ui_scan_status(request: Request, scan_id: str = "") -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()
    scan = db_get_scan(engine, scan_id)

    if scan is None:
        return HTMLResponse('<p style="color:#dc2626">Scan not found.</p>')

    if scan.status == "running":
        return templates.TemplateResponse(
            request, "ui/partials/scan_progress.html",
            {
                "scan_id": scan_id,
                "entity_name": scan.entity_name,
                "windows_total": scan.windows_total,
                "windows_done": scan.windows_done,
                "effective_start": "",
                "end": "",
            },
        )

    results = json.loads(scan.results_json)
    return templates.TemplateResponse(
        request, "ui/partials/scan_result.html",
        {
            "entity_name": scan.entity_name,
            "status": scan.status,
            "results": results,
        },
    )


# ── Details / timing routes ───────────────────────────────────────────────────


def _bullet_stats(session, run_id) -> dict:
    """Return bullet counts from SQLBulletRunLog for a given run_id."""
    rows = session.exec(
        select(SQLBulletRunLog).where(SQLBulletRunLog.run_id == run_id)
    ).all()
    if not rows:
        return {"total": 0, "active": 0, "discarded": 0, "stages": {}}
    active = sum(1 for r in rows if r.is_active)
    stages: dict[str, int] = {}
    for r in rows:
        if not r.is_active and r.discard_stage:
            stages[r.discard_stage] = stages.get(r.discard_stage, 0) + 1
    return {
        "total": len(rows),
        "active": active,
        "discarded": len(rows) - active,
        "stages": stages,
    }


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    s = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
    e = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    return max(0.0, (e - s).total_seconds())


def _scan_aggregate_costs(session: Session, results: list[dict]) -> tuple[float | None, float | None]:
    """Sum LLM+embedding USD and chunk/API-search USD from metrics for each window ``run_id``."""
    ids: list[uuid.UUID] = []
    for r in results:
        rid = r.get("run_id")
        if not rid:
            continue
        try:
            ids.append(uuid.UUID(str(rid)))
        except ValueError:
            continue
    if not ids:
        return None, None
    ids = list(dict.fromkeys(ids))
    metrics = session.exec(select(SQLRunMetrics).where(SQLRunMetrics.run_id.in_(ids))).all()
    by_run = {m.run_id: m for m in metrics}
    llm = 0.0
    api = 0.0
    for uid in ids:
        m = by_run.get(uid)
        if not m:
            continue
        llm += float(m.total_llm_cost_usd or 0.0) + float(m.total_embedding_cost_usd or 0.0)
        api += float(calculate_chunk_cost(m.chunks_total or 0))
    return round(llm, 6), round(api, 6)


def _entity_runs_in_window(
    session,
    entity_ids: list[str],
    window_start: datetime,
    window_end: datetime,
) -> list[SQLEntityPipelineRunLog]:
    """Return run logs for a set of entities started within [window_start, window_end]."""
    # Pad slightly to catch runs started just before batch creation
    pad_start = window_start.replace(tzinfo=timezone.utc) if window_start.tzinfo is None else window_start
    pad_end = window_end.replace(tzinfo=timezone.utc) if window_end.tzinfo is None else window_end
    from datetime import timedelta
    pad_start = pad_start - timedelta(minutes=1)
    pad_end = pad_end + timedelta(minutes=5)

    rows = session.exec(
        select(SQLEntityPipelineRunLog)
        .where(SQLEntityPipelineRunLog.entity_id.in_(entity_ids))
        .where(SQLEntityPipelineRunLog.process_started_at_utc >= pad_start)
        .where(SQLEntityPipelineRunLog.process_started_at_utc <= pad_end)
        .order_by(SQLEntityPipelineRunLog.process_started_at_utc)
    ).all()
    return list(rows)


@router.get("/details", response_class=HTMLResponse)
async def ui_details_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    engine = get_engine()

    with Session(engine) as session:
        ui_batches = session.exec(
            select(SQLUIBatchRun).order_by(desc(SQLUIBatchRun.created_at)).limit(50)
        ).all()
        api_batches = session.exec(
            select(SQLBatchParallelRun).order_by(desc(SQLBatchParallelRun.submitted_at)).limit(50)
        ).all()
        scans_raw = session.exec(
            select(SQLUIScanRun).order_by(desc(SQLUIScanRun.created_at)).limit(50)
        ).all()
        entity_runs = session.exec(
            select(SQLEntityPipelineRunLog)
            .order_by(desc(SQLEntityPipelineRunLog.process_started_at_utc))
            .limit(200)
        ).all()
        orch_map: dict[str, str] = {
            r.entity_id: (r.kg_name or r.entity_id)
            for r in session.exec(select(SQLEntityOrchestrationState)).all()
        }
        run_ids = [r.run_id for r in entity_runs]
        metrics_map: dict[str, SQLRunMetrics] = {}
        if run_ids:
            for m in session.exec(
                select(SQLRunMetrics).where(SQLRunMetrics.run_id.in_(run_ids))
            ).all():
                metrics_map[str(m.run_id)] = m

        scans_enriched: list[dict] = []
        for s in scans_raw:
            dur = _duration_seconds(s.created_at, s.updated_at)
            results = json.loads(s.results_json)
            llm_c, api_c = _scan_aggregate_costs(session, results)
            scans_enriched.append({
                "id": s.scan_id,
                "entity_id": s.entity_id,
                "entity_name": s.entity_name,
                "started": s.created_at,
                "status": s.status,
                "windows_total": s.windows_total,
                "windows_done": s.windows_done,
                "duration": _fmt_duration(dur),
                "duration_s": dur,
                "results": results,
                "cost_llm": llm_c,
                "cost_api": api_c,
            })

    def _enrich_ui_batch(b: SQLUIBatchRun) -> dict:
        dur = _duration_seconds(b.created_at, b.updated_at)
        results = json.loads(b.results_json)
        return {
            "id": b.batch_id,
            "type": "UI Parallel",
            "started": b.created_at,
            "status": b.status,
            "total": b.total,
            "done": b.done,
            "duration": _fmt_duration(dur),
            "duration_s": dur,
            "entity_ids": json.loads(b.entity_ids_json),
            "window_start": b.created_at,
            "window_end": b.updated_at,
            "results": results,
        }

    def _enrich_api_batch(b: SQLBatchParallelRun) -> dict:
        eids = json.loads(b.entity_ids_json)
        run_ids_map: dict = json.loads(b.run_ids_json)
        return {
            "id": str(b.batch_id),
            "type": "API Parallel",
            "started": b.submitted_at,
            "status": "submitted",
            "total": b.total,
            "done": b.total,
            "duration": "—",
            "duration_s": None,
            "entity_ids": eids,
            "window_start": b.submitted_at,
            "window_end": None,
            "run_ids_map": run_ids_map,
        }

    def _enrich_run(r: SQLEntityPipelineRunLog) -> dict:
        dur = _duration_seconds(r.process_started_at_utc, r.process_completed_at_utc)
        with Session(engine) as _s:
            stats = _bullet_stats(_s, r.run_id)
        m = metrics_map.get(str(r.run_id))
        chunks = m.chunks_total if m else 0
        chunk_cost = calculate_chunk_cost(chunks)
        llm_cost = m.total_llm_cost_usd if m else None
        emb_cost = m.total_embedding_cost_usd if m else None
        total_cost = round((llm_cost or 0) + (emb_cost or 0) + chunk_cost, 6) if m else None
        return {
            "run_id": str(r.run_id),
            "entity_id": r.entity_id,
            "entity_name": orch_map.get(r.entity_id, r.entity_id),
            "started": r.process_started_at_utc,
            "completed": r.process_completed_at_utc,
            "status": r.status,
            "window_start": r.report_window_start,
            "window_end": r.report_window_end,
            "duration": _fmt_duration(dur),
            "duration_s": dur,
            "bullets_total": stats["total"],
            "bullets_active": stats["active"],
            "bullets_discarded": stats["discarded"],
            "discard_stages": stats["stages"],
            "cost_llm": llm_cost,
            "cost_embedding": emb_cost,
            "cost_chunks": chunk_cost if m else None,
            "chunks_total": chunks if m else None,
            "cost_total": total_cost,
        }

    return templates.TemplateResponse(
        request, "ui/details.html",
        {
            "ui_batches": [_enrich_ui_batch(b) for b in ui_batches],
            "api_batches": [_enrich_api_batch(b) for b in api_batches],
            "scans": scans_enriched,
            "entity_runs": [_enrich_run(r) for r in entity_runs],
            "orch_map": orch_map,
        },
    )


@router.get("/partials/batch-detail", response_class=HTMLResponse)
async def ui_batch_detail_partial(request: Request, batch_id: str = "", batch_type: str = "") -> HTMLResponse:
    """Expand per-entity run details for a batch row."""
    engine = get_engine()

    with Session(engine) as session:
        orch_map: dict[str, str] = {
            r.entity_id: (r.kg_name or r.entity_id)
            for r in session.exec(select(SQLEntityOrchestrationState)).all()
        }

        if batch_type == "ui":
            batch = session.get(SQLUIBatchRun, batch_id)
            if not batch:
                return HTMLResponse("<td colspan='6'><em>Not found.</em></td>")
            eids = json.loads(batch.entity_ids_json)
            runs = _entity_runs_in_window(session, eids, batch.created_at, batch.updated_at)
        elif batch_type == "api":
            import uuid as _uuid
            try:
                bid = _uuid.UUID(batch_id)
            except ValueError:
                return HTMLResponse("<td colspan='6'><em>Invalid ID.</em></td>")
            batch = session.get(SQLBatchParallelRun, bid)
            if not batch:
                return HTMLResponse("<td colspan='6'><em>Not found.</em></td>")
            eids = json.loads(batch.entity_ids_json)
            window_start = batch.submitted_at
            # Use 24h window as upper bound since API batches don't track end
            from datetime import timedelta
            window_end = batch.submitted_at + timedelta(hours=24)
            runs = _entity_runs_in_window(session, eids, window_start, window_end)
        else:
            return HTMLResponse("<td colspan='6'><em>Unknown batch type.</em></td>")

    rows_html = ""
    for r in runs:
        dur = _duration_seconds(r.process_started_at_utc, r.process_completed_at_utc)
        with Session(engine) as _s:
            stats = _bullet_stats(_s, r.run_id)
        name = html.escape(orch_map.get(r.entity_id, r.entity_id))
        eid = html.escape(r.entity_id)
        started = r.process_started_at_utc.strftime("%H:%M:%S") if r.process_started_at_utc else "—"
        completed = r.process_completed_at_utc.strftime("%H:%M:%S") if r.process_completed_at_utc else "running"
        status_cls = {"succeeded": "color:#166534", "failed": "color:#dc2626", "running": "color:#2563eb"}.get(r.status, "")
        bullets_html = ""
        if stats["total"]:
            bullets_html = (
                f'<span style="color:#166534;font-weight:600">{stats["active"]}✓</span> '
                f'<span style="color:#dc2626">{stats["discarded"]}✗</span>'
            )
        rows_html += (
            f'<tr style="background:#f8fafc">'
            f'<td style="padding:.35rem 1rem .35rem 2.5rem;font-size:.82rem;color:var(--muted)">↳ {name} <span style="font-family:monospace;font-size:.75rem">({eid})</span></td>'
            f'<td style="padding:.35rem .75rem;font-size:.82rem">{started}</td>'
            f'<td style="padding:.35rem .75rem;font-size:.82rem">{completed}</td>'
            f'<td style="padding:.35rem .75rem;font-size:.82rem;font-weight:600;{status_cls}">{r.status}</td>'
            f'<td style="padding:.35rem .75rem;font-size:.82rem;font-weight:600">{_fmt_duration(dur)}</td>'
            f'<td style="padding:.35rem .75rem;font-size:.82rem">{bullets_html}</td>'
            f'</tr>'
        )
    if not rows_html:
        rows_html = '<tr style="background:#f8fafc"><td colspan="6" style="padding:.5rem 2.5rem;font-size:.82rem;color:var(--muted)">No individual run records found for this batch.</td></tr>'
    return HTMLResponse(rows_html)


@router.get("/partials/scan-detail", response_class=HTMLResponse)
async def ui_scan_detail_partial(request: Request, scan_id: str = "") -> HTMLResponse:
    """Expand per-window scan rows: pipeline run id, status, and LLM / API-search cost per window."""
    engine = get_engine()
    with Session(engine) as session:
        scan = session.get(SQLUIScanRun, scan_id)
        if not scan:
            return HTMLResponse(
                '<p style="padding:.5rem 1rem;margin:0;font-size:.82rem;color:var(--muted)"><em>Not found.</em></p>'
            )

        results = json.loads(scan.results_json)
        body_rows = ""
        for r in results:
            ws = (r.get("window_start") or "")[:16].replace("T", " ")
            we = (r.get("window_end") or "")[:16].replace("T", " ")
            status = r.get("status", "") or "—"
            status_cls = {
                "succeeded": "color:#166534",
                "failed": "color:#dc2626",
                "cancelled": "color:#92400e",
                "skipped": "color:#64748b",
            }.get(status, "")
            err = html.escape(r.get("error") or "")
            rid = r.get("run_id")
            llm_s = "—"
            api_s = "—"
            run_cell = '<span style="color:var(--muted)">—</span>'
            if rid:
                rid_raw = str(rid)
                run_cell = (
                    f'<a class="mono" href="/ui/cost-details/{html.escape(rid_raw, quote=True)}" '
                    f'style="font-size:.75rem">{html.escape(rid_raw[:8])}…</a>'
                )
                try:
                    uid = uuid.UUID(rid_raw)
                except ValueError:
                    uid = None
                if uid is not None:
                    m = session.exec(select(SQLRunMetrics).where(SQLRunMetrics.run_id == uid)).first()
                    if m:
                        llm_v = float(m.total_llm_cost_usd or 0) + float(m.total_embedding_cost_usd or 0)
                        api_v = float(calculate_chunk_cost(m.chunks_total or 0))
                        llm_s = f"${llm_v:.4f}"
                        api_s = f"${api_v:.4f}"
            body_rows += (
                "<tr>"
                f'<td style="padding:.35rem .75rem;font-size:.82rem;color:var(--muted)">{html.escape(ws)} → {html.escape(we)}</td>'
                f'<td style="padding:.35rem .75rem">{run_cell}</td>'
                f'<td style="padding:.35rem .75rem;font-weight:600;{status_cls}">{html.escape(status)}</td>'
                f'<td style="padding:.35rem .75rem;font-size:.78rem;color:#dc2626;max-width:22rem;word-break:break-word">{err}</td>'
                f'<td style="padding:.35rem .75rem;font-variant-numeric:tabular-nums">{llm_s}</td>'
                f'<td style="padding:.35rem .75rem;font-variant-numeric:tabular-nums">{api_s}</td>'
                "</tr>"
            )
        if not body_rows:
            body_rows = (
                '<tr><td colspan="6" style="padding:.5rem 1rem;font-size:.82rem;color:var(--muted)">'
                "No window results yet.</td></tr>"
            )
        html_out = (
            '<table style="width:100%;font-size:.82rem;border-collapse:collapse;margin:0;background:#f8fafc">'
            "<thead><tr>"
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">Window (UTC)</th>'
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">Run</th>'
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">Status</th>'
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">Error</th>'
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">LLM+emb</th>'
            '<th style="text-align:left;padding:.35rem .75rem;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">API search</th>'
            "</tr></thead><tbody>"
            f"{body_rows}</tbody></table>"
        )
    return HTMLResponse(html_out)


_STEP_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Phase 1 — Search",        ["exploratory_search", "concept_search"]),
    ("Phase 2 — Bullets",       ["bullets_generation", "relevance_score"]),
    ("Grounding",               ["entity_grounding_check"]),
    ("Novelty Embedding",       ["llm_novelty_window", "llm_remaining_window", "llm_full_history"]),
    ("Novelty Search",          ["novelty_search"]),
    ("Concept Extraction",      ["concept_extraction"]),
    ("Post-processing",         ["thematic_clustering", "redundancy", "standalone", "validator", "consolidate"]),
]


def _categorize_steps(step_detail: dict) -> list[dict]:
    """Group step_detail entries by pipeline category. Returns list of
    {label, steps: [(name, data)], totals: {...}} dicts."""
    assigned: set[str] = set()
    groups: list[dict] = []

    for label, prefixes in _STEP_CATEGORIES:
        matched = [
            (name, data)
            for name, data in step_detail.items()
            if any(name.startswith(p) for p in prefixes) and name not in assigned
        ]
        if not matched:
            continue
        for name, _ in matched:
            assigned.add(name)

        totals: dict = {
            "llm_cost_usd": 0.0, "llm_prompt_tokens": 0, "llm_completion_tokens": 0,
            "llm_calls": 0, "embedding_cost_usd": 0.0, "embedding_tokens": 0,
            "api_calls": 0, "api_query_units": 0.0, "chunks_retrieved": 0,
            "total_cost_usd": 0.0,
        }
        for _, data in matched:
            for k in ("llm_cost_usd", "llm_prompt_tokens", "llm_completion_tokens",
                      "llm_calls", "embedding_cost_usd", "embedding_tokens", "total_cost_usd"):
                totals[k] += data.get(k, 0)
            ops = data.get("operational") or {}
            totals["api_calls"] += ops.get("api_calls", 0)
            totals["api_query_units"] += ops.get("api_query_units", 0.0)
            totals["chunks_retrieved"] += ops.get("chunks_retrieved", 0)

        groups.append({"label": label, "steps": matched, "totals": totals})

    # Uncategorized remainder
    remainder = [(n, d) for n, d in step_detail.items() if n not in assigned]
    if remainder:
        totals = {
            "llm_cost_usd": 0.0, "llm_prompt_tokens": 0, "llm_completion_tokens": 0,
            "llm_calls": 0, "embedding_cost_usd": 0.0, "embedding_tokens": 0,
            "api_calls": 0, "api_query_units": 0.0, "chunks_retrieved": 0,
            "total_cost_usd": 0.0,
        }
        for _, data in remainder:
            for k in ("llm_cost_usd", "llm_prompt_tokens", "llm_completion_tokens",
                      "llm_calls", "embedding_cost_usd", "embedding_tokens", "total_cost_usd"):
                totals[k] += data.get(k, 0)
            ops = data.get("operational") or {}
            totals["api_calls"] += ops.get("api_calls", 0)
            totals["api_query_units"] += ops.get("api_query_units", 0.0)
            totals["chunks_retrieved"] += ops.get("chunks_retrieved", 0)
        groups.append({"label": "Other", "steps": remainder, "totals": totals})

    return groups


@router.get("/cost-details/{run_id}", response_class=HTMLResponse)
async def ui_cost_details_page(request: Request, run_id: str) -> HTMLResponse:
    """Cost breakdown for a single pipeline run."""
    templates = request.app.state.templates
    engine = get_engine()

    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return HTMLResponse("<p>Invalid run ID.</p>", status_code=400)

    with Session(engine) as session:
        run = session.get(SQLEntityPipelineRunLog, run_uuid)
        metrics = session.exec(
            select(SQLRunMetrics).where(SQLRunMetrics.run_id == run_uuid)
        ).first()
        orch = session.get(SQLEntityOrchestrationState, run.entity_id) if run else None

    if not run:
        return HTMLResponse("<p>Run not found.</p>", status_code=404)

    entity_name = (orch.kg_name if orch else None) or run.entity_id
    dur = _duration_seconds(run.process_started_at_utc, run.process_completed_at_utc)

    llm_models: list[dict] = []
    step_detail: dict = {}
    step_groups: list[dict] = []
    chunks_total = 0
    chunk_cost = 0.0
    total_llm_cost = 0.0
    total_emb_cost = 0.0
    embedding_model = "N/A"
    embedding_tokens = 0

    if metrics:
        llm_models = json.loads(metrics.llm_per_model_json or "[]")
        step_detail = json.loads(metrics.step_detail_json or "{}")
        step_groups = _categorize_steps(step_detail)
        chunks_total = metrics.chunks_total
        chunk_cost = calculate_chunk_cost(chunks_total)
        total_llm_cost = metrics.total_llm_cost_usd
        total_emb_cost = metrics.total_embedding_cost_usd
        embedding_model = metrics.embedding_model
        embedding_tokens = metrics.embedding_tokens

    total_cost = round(total_llm_cost + total_emb_cost + chunk_cost, 6)

    return templates.TemplateResponse(
        request, "ui/cost_details.html",
        {
            "run_id": run_id,
            "entity_name": entity_name,
            "entity_id": run.entity_id,
            "window_start": run.report_window_start,
            "window_end": run.report_window_end,
            "status": run.status,
            "duration": _fmt_duration(dur),
            "has_metrics": metrics is not None,
            "llm_models": llm_models,
            "step_detail": step_detail,
            "step_groups": step_groups,
            "chunks_total": chunks_total,
            "chunk_cost": chunk_cost,
            "total_llm_cost": total_llm_cost,
            "total_emb_cost": total_emb_cost,
            "total_cost": total_cost,
            "embedding_model": embedding_model,
            "embedding_tokens": embedding_tokens,
        },
    )

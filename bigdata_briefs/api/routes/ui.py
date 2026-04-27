"""
UI routes — HTMX-powered browser interface.

Pages (full HTML):
    GET  /ui/run              → Run Brief (form + live results)
    GET  /ui/history          → Company History (clean, passed bullets only)
    GET  /ui/history-details  → Company History (full detail + discards)

HTMX partials (HTML fragments):
    POST /ui/batch/run                → trigger batch; returns progress fragment
    POST /ui/batch/stop               → set cancel event for a running batch
    GET  /ui/partials/run-status      → live progress / final results (polled every 3s)
    GET  /ui/partials/history         → bullet history for a selected entity
    GET  /ui/partials/history-details → bullet history + full details for a selected entity
"""

from __future__ import annotations

import html
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc
from sqlmodel import Session, select

from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_engine,
    get_entity_executor,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.api.routes.universes import _UNIVERSES
from bigdata_briefs.orchestration.config_load import load_pipeline_config_dict, resolve_config_path
from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLUIBatchRun,
)
from bigdata_briefs.orchestration.windows import WindowMode, build_report_dates_for_entity_run
from bigdata_briefs.settings import settings

_ENTITY_STAGGER_SECONDS = 3.0

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
    bullet_points: list[dict] = field(default_factory=list)
    source_references: dict = field(default_factory=dict)


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
            "bullet_points": result.bullet_points,
            "source_references": result.source_references,
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
    startup_delay_seconds: float = 0.0,
) -> None:
    """Worker for one entity inside the parallel UI batch."""
    import time as _time
    if startup_delay_seconds > 0:
        _time.sleep(startup_delay_seconds)

    if _db_is_cancelled(engine, batch_id):
        _db_append_result(engine, batch_id, EntityRunStatus(
            entity_id=entity_id,
            entity_name=_get_entity_name(engine, entity_id),
            status="cancelled",
        ))
        return

    pipeline_config = load_pipeline_config_dict(resolve_config_path(None))
    state_dir = Path(".brief_pipeline_state")
    entity_name = _get_entity_name(engine, entity_id)

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
        _db_append_result(engine, batch_id, EntityRunStatus(
            entity_id=entity_id,
            entity_name=entity_name,
            status="failed",
            error=str(exc),
        ))
        return

    bullet_points: list[dict] = []
    source_references: dict = {}
    if result.success and result.run_id:
        with Session(engine) as session:
            log = session.get(SQLEntityPipelineRunLog, result.run_id)
            if log and log.output_json:
                try:
                    data = json.loads(log.output_json)
                    if isinstance(data, dict):
                        bullet_points = data.get("bullet_points") or []
                        source_references = data.get("source_references") or {}
                except (json.JSONDecodeError, TypeError):
                    pass

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
        bullet_points=bullet_points,
        source_references=source_references,
    ))


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
) -> None:
    """Submit all entities to the shared ThreadPoolExecutor in parallel with stagger.

    Each entity writes its result to the DB when it completes, so the polling
    route always reflects the current state even if entities finish out of order.
    When all futures are done the batch is marked finished.
    """
    import threading as _threading
    total = len(entity_ids)
    done_count = [0]
    lock = _threading.Lock()

    def _on_done(_future):
        with lock:
            done_count[0] += 1
            if done_count[0] == total:
                _db_finish_batch(engine, batch_id)

    for idx, entity_id in enumerate(entity_ids):
        future = executor.submit(
            _run_one_ui_entity,
            batch_id=batch_id,
            entity_id=entity_id,
            force_window_end=force_window_end,
            engine=engine,
            rate_limiter=rate_limiter,
            connection_sem=connection_sem,
            http_client=http_client,
            startup_delay_seconds=idx * _ENTITY_STAGGER_SECONDS,
        )
        future.add_done_callback(_on_done)


# ── Data helpers ──────────────────────────────────────────────────────────────


def _load_run_log_data(log: SQLEntityPipelineRunLog) -> tuple[list[dict], dict]:
    """Parse output_json from a run log row into (bullet_points, source_references)."""
    if not log.output_json:
        return [], {}
    try:
        data = json.loads(log.output_json)
        if isinstance(data, dict):
            return data.get("bullet_points") or [], data.get("source_references") or {}
        return [], {}
    except (json.JSONDecodeError, TypeError):
        return [], {}


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
    not_fully_novel = bool(is_active and overall_verdict in ("mixed", "mixed_noise"))

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
        score_html = f'<span class="score-pill">Score {int(rs)}/5</span>' if isinstance(rs, (int, float)) else ""
        detail_parts.append(
            f'<div class="detail-panel"><div class="detail-label">Relevance (passed){score_html}</div>'
            f'<div class="detail-reason">{_nl_to_br(rr or "—")}</div></div>'
        )
        if original and final and original != final:
            detail_parts.append(
                '<div class="detail-panel"><div class="detail-label">Original draft</div>'
                f'<div class="tx-body">{_nl_to_br(original)}</div></div>'
            )
        if citations:
            detail_parts.append(
                '<div class="detail-panel"><div class="detail-label">Sources</div>'
                f'{_render_citation_cards(citations)}</div>'
            )
        inner = "".join(detail_parts)
        if inner.strip():
            parts.append(_wrap_details_expander(inner, f"{bid}-det"))

    parts.append("</li>")
    return "".join(parts)


def _render_discarded_detail_body(b: dict) -> str:
    d = b.get("discarded") or {}
    stage = str(d.get("stage") or "unknown")
    reason = str(d.get("reason") or "").strip()
    parts = [
        '<div class="detail-panel">',
        f'<div class="detail-label">Stage: {html.escape(_DISCARD_STAGE_LABELS.get(stage, stage))}</div>',
        f'<div class="detail-reason">{_nl_to_br(reason or "—")}</div>',
    ]
    score = d.get("score")
    if isinstance(score, (int, float)):
        parts.append(f'<div class="detail-block"><div class="detail-label">Score</div>'
                     f'<div class="detail-body"><span class="score-pill">Score {int(score)}/5</span></div></div>')

    # Text compare for rewrite stages
    original = str(b.get("original_text") or "").strip()
    final = str(b.get("final_text") or "").strip()
    if stage in ("novelty_embedding_relevance", "novelty_search_relevance") and original and final and original != final:
        parts.append(
            '<div class="detail-block"><div class="detail-label">Original vs rewritten</div>'
            '<div class="text-compare">'
            f'<div class="text-compare-col"><h5>Original</h5><div class="detail-body">{_nl_to_br(original)}</div></div>'
            f'<div class="text-compare-col"><h5>After rewrite</h5><div class="detail-body">{_nl_to_br(final)}</div></div>'
            '</div></div>'
        )

    # Evaluator details (novelty_embedding stage)
    for ev in (d.get("evaluator_details") or []):
        if not isinstance(ev, dict):
            continue
        ename = html.escape(str(ev.get("evaluator_name") or "evaluator"))
        decision = html.escape(str(ev.get("decision") or ""))
        ev_reason = str(ev.get("reason") or "").strip()
        parts.append(
            f'<div class="evaluator-block"><div class="detail-label">{ename} ({decision})</div>'
        )
        if ev_reason:
            parts.append(f'<div class="detail-reason">{_nl_to_br(ev_reason)}</div>')
        for rb in (ev.get("retrieved_bullets") or []):
            if not isinstance(rb, dict):
                continue
            rb_text = str(rb.get("text") or "")
            rb_score = rb.get("score")
            rb_date = str(rb.get("date") or "")
            score_s = f"{float(rb_score):.3f}" if isinstance(rb_score, (int, float)) else "—"
            parts.append(
                f'<div class="retrieved-bullet-card">'
                f'<div class="retrieved-bullet-meta">similarity {score_s} · {html.escape(rb_date)}</div>'
                f'<div class="tx-body">{_nl_to_br(rb_text)}</div></div>'
            )
        parts.append("</div>")

    # Claim verdicts (novelty_search stage)
    ov = str(d.get("overall_verdict") or "").strip()
    if ov:
        parts.append(
            f'<div class="detail-block"><div class="detail-label">Overall verdict</div>'
            f'<div class="detail-body">{html.escape(ov)}</div></div>'
        )
    for cv in (d.get("claim_verdicts") or []):
        if not isinstance(cv, dict):
            continue
        idx = cv.get("claim_index")
        ctext = str(cv.get("claim_text") or "")
        nov = str(cv.get("novelty") or "")
        rsn = str(cv.get("reasoning") or "").strip()
        idx_s = html.escape(str(idx)) if idx is not None else "—"
        parts.append(
            f'<div class="claim-block">'
            f'<div class="claim-meta">Claim #{idx_s} · novelty: <strong>{html.escape(nov)}</strong></div>'
            f'<div class="detail-body">{_nl_to_br(ctext.strip() or "—")}</div>'
        )
        if rsn:
            parts.append(f'<div class="detail-block"><div class="detail-label">Reasoning</div>'
                         f'<div class="detail-reason">{_nl_to_br(rsn)}</div></div>')
        for ev in (cv.get("evidence") or []):
            if not isinstance(ev, dict):
                continue
            hl = str(ev.get("headline") or "")
            tx = str(ev.get("text") or "")
            dt = str(ev.get("date") or "")
            sid = str(ev.get("simple_id") or "—")
            parts.append(
                f'<div class="evidence-card">'
                f'<div class="source-line"><strong>{html.escape(sid)}</strong> · {html.escape(dt)}</div>'
                f'<div class="hl-label">Headline</div><div class="hl-body">{_nl_to_br(hl or "—")}</div>'
                f'<div class="tx-label">Text</div><div class="tx-body">{_nl_to_br(tx)}</div>'
                f'</div>'
            )
        parts.append("</div>")

    parts.append("</div>")
    return "".join(parts)


def _render_discarded_section(discarded: list[dict], section_id: str) -> str:
    if not discarded:
        return ""
    buckets: dict[str, list[dict]] = {}
    for b in discarded:
        stage = _get_discard_stage(b)
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
    bullet_points: list[dict],
    source_refs: dict,
    run_key: str,
    *,
    include_discarded: bool,
    include_details: bool,
) -> str:
    converted = [_convert_bp(bp, source_refs) for bp in bullet_points]
    active = [b for b in converted if b.get("is_active", True)]
    discarded = [b for b in converted if not b.get("is_active", True)]

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
            bps, src_refs = _load_run_log_data(log)
            run_key = f"hist-{log.run_id}-{run_idx}"
            parts.append(_render_run_bullets_html(
                bps, src_refs, run_key,
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

    # finished or cancelled — deserialise results and render final HTML
    statuses: list[EntityRunStatus] = []
    for r in json.loads(batch.results_json):
        statuses.append(EntityRunStatus(
            entity_id=r["entity_id"],
            entity_name=r["entity_name"],
            status=r["status"],
            error=r.get("error"),
            window_start=r.get("window_start"),
            window_end=r.get("window_end"),
            bullet_points=r.get("bullet_points") or [],
            source_references=r.get("source_references") or {},
        ))

    results_html = _render_batch_results(statuses)
    return templates.TemplateResponse(
        request, "ui/partials/run_result.html",
        {"results_html": results_html},
    )


def _render_batch_results(statuses: list[EntityRunStatus]) -> str:
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
            bullets_html = _render_run_bullets_html(
                s.bullet_points, s.source_references, run_key,
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
    history_html = _render_entity_history_html(runs, include_discarded=False, include_details=False)
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
    history_html = _render_entity_history_html(runs, include_discarded=True, include_details=True)
    return templates.TemplateResponse(
        request, "ui/partials/history_content.html",
        {"history_html": history_html},
    )

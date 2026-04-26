"""Report endpoints — generate HTML briefs from pipeline output."""

from __future__ import annotations

import json
from sqlalchemy import desc
from sqlmodel import Session, select

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import get_engine
from bigdata_briefs.api.routes.batch import _all_entity_ids, _build_bullet_detail
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)
from bigdata_briefs.report.html_builder import build_html

router = APIRouter(tags=["report"])


def _build_doc_index(source_refs: dict) -> dict:
    """Build two-level index from source_references for resolving citation IDs.

    Citation IDs have format 'CQS:{document_id}-{chunk}'.
    source_references keys are 'CQS:REF0' etc. with document_id + chunk_id inside.
    We index by '{document_id}-{chunk_id}' so we can resolve exact citations,
    and also by '{document_id}' as a fallback.
    """
    index: dict = {}
    for ref in source_refs.values():
        doc_id = str(ref.get("document_id") or "").strip()
        chunk_id = ref.get("chunk_id")
        if not doc_id:
            continue
        ts = str(ref.get("ts") or "").replace("T", " ")[:19]  # YYYY-MM-DD HH:MM:SS
        entry = {
            "headline": ref.get("headline") or "",
            "text": ref.get("text") or "",
            "source_name": ref.get("source_name") or "",
            "date": ts,
        }
        # Exact key: document_id-chunk_id
        if chunk_id is not None:
            exact_key = f"{doc_id}-{chunk_id}"
            if exact_key not in index:
                index[exact_key] = entry
        # Fallback key: document_id only
        if doc_id not in index:
            index[doc_id] = entry
    return index


def _resolve_citation(cit_id: str, doc_index: dict) -> dict:
    """Resolve a citation ID to {id, headline, text, source_name}."""
    tail = cit_id.split(":", 1)[-1]          # strip 'CQS:' prefix
    doc_id_only = tail.rsplit("-", 1)[0]      # strip '-{chunk}' suffix
    meta = doc_index.get(tail) or doc_index.get(doc_id_only) or {}
    return {
        "id": cit_id,
        "headline": meta.get("headline") or "",
        "text": meta.get("text") or "",
        "source_name": meta.get("source_name") or "",
        "date": meta.get("date") or "",
    }


def _build_trace_citations(entity_id: str, engine) -> dict:
    """Build trace_id → list[{id, headline, text, source_name}] from generated_bullet_points.

    Queries ALL runs for the entity and indexes by trace_id (UUIDs unique per
    bullet) so that citations are found regardless of run_id mismatches.
    """
    from bigdata_briefs.novelty.storage import SQLiteGeneratedBulletPointStorage
    storage = SQLiteGeneratedBulletPointStorage(engine)
    all_runs = storage.get_all_runs_bullets(entity_id)
    result: dict = {}
    for rows in all_runs.values():
        for row in rows:
            if not row.trace_id or not row.citations:
                continue
            try:
                cits = row.citations if isinstance(row.citations, list) else json.loads(row.citations)
                result[row.trace_id] = [
                    {
                        "id": c.get("id", ""),
                        "headline": c.get("headline", ""),
                        "text": c.get("text", ""),
                        "source_name": c.get("source_name") or "",
                    }
                    for c in (cits or []) if isinstance(c, dict)
                ]
            except Exception:
                pass
    return result


def _build_entity_dict(entity_id: str, engine) -> dict:
    """Build entity dict for HTML rendering using _build_bullet_detail logic."""
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        entity_name: str | None = orch.kg_name if orch else None

        row = session.exec(
            select(SQLEntityPipelineRunLog)
            .where(SQLEntityPipelineRunLog.entity_id == entity_id)
            .where(SQLEntityPipelineRunLog.status == "succeeded")
            .order_by(desc(SQLEntityPipelineRunLog.process_completed_at_utc))
        ).first()

    if not row or not row.output_json:
        return {"entity_id": entity_id, "found": False}

    try:
        parsed = json.loads(row.output_json)
        raw_bullets: list[dict] = parsed if isinstance(parsed, list) else parsed.get("bullet_points") or []
        source_refs: dict = {} if isinstance(parsed, list) else parsed.get("source_references") or {}
    except (json.JSONDecodeError, TypeError):
        return {"entity_id": entity_id, "found": False}

    doc_index = _build_doc_index(source_refs)
    run_id = str(row.run_id)

    # Full citations (with text) for active bullets from generated_bullet_points
    trace_citations = _build_trace_citations(entity_id, engine)

    bullets: list[dict] = []
    for bp in raw_bullets:
        bullet = _build_bullet_detail(bp).model_dump()

        trace_id = bullet.get("trace_id", "")
        is_active = bullet.get("is_active", True)

        # original_text for detail-mode detection
        gen = bp.get("generation") or {}
        if "original_text" not in bullet or not bullet.get("original_text"):
            bullet["original_text"] = gen.get("original_text") or bp.get("text", "")

        if is_active and trace_id in trace_citations:
            # Active bullet: full citations from generated_bullet_points + date from source_refs
            enriched_cits = []
            for c in trace_citations[trace_id]:
                cid = c.get("id", "")
                tail = cid.split(":", 1)[-1]
                meta = doc_index.get(tail) or doc_index.get(tail.rsplit("-", 1)[0]) or {}
                enriched_cits.append({**c, "date": meta.get("date") or ""})
            bullet["citations"] = enriched_cits
        else:
            # Discarded bullet: resolve from source_references (has both headline and text)
            raw_cit_ids = [
                c if isinstance(c, str) else c.get("id", "")
                for c in (bp.get("citations") or [])
            ]
            bullet["citations"] = [
                _resolve_citation(cid, doc_index)
                for cid in raw_cit_ids if cid
            ]

        bullets.append(bullet)

    return {
        "entity_id": entity_id,
        "found": True,
        "entity_name": entity_name,
        "runs": [{
            "run_id": run_id,
            "report_window_start": str(row.report_window_start),
            "report_window_end": str(row.report_window_end),
            "total_bullets": len(bullets),
            "bullets": bullets,
        }],
    }


@router.get(
    "/report/html",
    response_class=HTMLResponse,
    dependencies=[Depends(require_api_key)],
    summary="Generate HTML brief report",
    description=(
        "Returns a self-contained HTML page with all bullets for the given entity "
        "(or all entities when ``entity_id`` is omitted). Active bullets show inline "
        "citation markers with full headline and text; discarded bullets show the "
        "discard stage and reasoning."
    ),
)
def get_report_html(entity_id: str | None = None) -> HTMLResponse:
    engine = get_engine()

    if entity_id:
        entity_ids = [entity_id]
        title = f"Brief Report — {entity_id}"
    else:
        entity_ids = _all_entity_ids(engine)
        title = "Brief Report — All Entities"

    results = [_build_entity_dict(eid, engine) for eid in entity_ids]
    data = {"results": results, "total_entities": len(results)}
    return HTMLResponse(content=build_html(data, title))

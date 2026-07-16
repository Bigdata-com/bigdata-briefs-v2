"""Assemble BriefDigest payloads from stateful DB rows or in-memory run results."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs.notifications.email_digest import (
    BriefDigest,
    DigestBullet,
    DigestCitation,
    DigestEntity,
)
from bigdata_briefs.orchestration.models import (
    SQLBulletRunLog,
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
)


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _parse_citations(raw: str | list | None) -> tuple[DigestCitation, ...]:
    if raw is None:
        return ()
    data: list[Any]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return ()
        data = parsed if isinstance(parsed, list) else []
    elif isinstance(raw, list):
        data = raw
    else:
        return ()

    citations: list[DigestCitation] = []
    for item in data:
        if isinstance(item, dict):
            citations.append(
                DigestCitation(
                    source_name=str(item.get("source_name") or ""),
                    headline=str(item.get("headline") or ""),
                    url=(str(item["url"]) if item.get("url") else None),
                )
            )
        elif isinstance(item, str) and item.strip():
            # Unresolved CQS ids — omit rather than show internal keys
            continue
    return tuple(citations)


def _entity_name_from_orch(engine: Engine, entity_id: str) -> str:
    with Session(engine) as session:
        orch = session.get(SQLEntityOrchestrationState, entity_id)
        if orch and orch.kg_name:
            return orch.kg_name
    return entity_id


def _bullets_for_run(engine: Engine, run_id: uuid.UUID) -> tuple[DigestBullet, ...]:
    with Session(engine) as session:
        rows = session.exec(
            select(SQLBulletRunLog)
            .where(SQLBulletRunLog.run_id == run_id)
            .where(SQLBulletRunLog.is_active == True)  # noqa: E712
            .order_by(SQLBulletRunLog.created_at)
        ).all()
    bullets: list[DigestBullet] = []
    for row in rows:
        text = (row.text or row.original_text or "").strip()
        if not text:
            continue
        bullets.append(
            DigestBullet(text=text, citations=_parse_citations(row.citations_json))
        )
    return tuple(bullets)


def digest_from_stateful_runs(
    engine: Engine,
    *,
    entity_ids: list[str],
    run_id_by_entity: dict[str, uuid.UUID | str | None],
) -> BriefDigest:
    """Build a digest for a batch of stateful entity runs.

    Includes every ``entity_id`` even when the run failed or produced no bullets.
    """
    entities: list[DigestEntity] = []
    window_start: datetime | None = None
    window_end: datetime | None = None

    with Session(engine) as session:
        for entity_id in entity_ids:
            run_id = _as_uuid(run_id_by_entity.get(entity_id))
            orch = session.get(SQLEntityOrchestrationState, entity_id)
            name = (orch.kg_name if orch and orch.kg_name else None) or entity_id

            if run_id is None:
                entities.append(
                    DigestEntity(
                        entity_id=entity_id,
                        entity_name=name,
                        error="No run_id recorded",
                    )
                )
                continue

            log = session.get(SQLEntityPipelineRunLog, run_id)
            if log is None:
                entities.append(
                    DigestEntity(
                        entity_id=entity_id,
                        entity_name=name,
                        error="Run log not found",
                    )
                )
                continue

            if log.report_window_start is not None:
                ws = log.report_window_start
                if window_start is None or ws < window_start:
                    window_start = ws
            if log.report_window_end is not None:
                we = log.report_window_end
                if window_end is None or we > window_end:
                    window_end = we

            if log.status == "failed":
                entities.append(
                    DigestEntity(
                        entity_id=entity_id,
                        entity_name=name,
                        error=log.error_summary or "pipeline failed",
                    )
                )
                continue

            bullets = _bullets_for_run(engine, run_id)
            entities.append(
                DigestEntity(entity_id=entity_id, entity_name=name, bullets=bullets)
            )

    return BriefDigest(
        entities=tuple(entities),
        window_start=window_start,
        window_end=window_end,
    )


def digest_from_entity_run_results(
    engine: Engine,
    results: list[Any],
) -> BriefDigest:
    """Build a digest from ``EntityRunResult`` objects (CLI sequential runs)."""
    entities: list[DigestEntity] = []
    window_start: datetime | None = None
    window_end: datetime | None = None

    for result in results:
        entity_id = str(result.entity_id)
        name = _entity_name_from_orch(engine, entity_id)
        rs = result.report_dates.start if result.report_dates else None
        re_ = result.report_dates.end if result.report_dates else None
        if rs is not None and (window_start is None or rs < window_start):
            window_start = rs
        if re_ is not None and (window_end is None or re_ > window_end):
            window_end = re_

        if not result.success:
            entities.append(
                DigestEntity(
                    entity_id=entity_id,
                    entity_name=name,
                    error=result.error or "pipeline failed",
                )
            )
            continue

        if result.run_id is not None:
            bullets = _bullets_for_run(engine, result.run_id)
        else:
            # Fallback: novelty-ok rows only have original_text
            bullets = tuple(
                DigestBullet(text=str(b.get("original_text") or "").strip())
                for b in (result.new_bullets_novelty_ok or [])
                if str(b.get("original_text") or "").strip()
            )

        entities.append(
            DigestEntity(entity_id=entity_id, entity_name=name, bullets=bullets)
        )

    return BriefDigest(
        entities=tuple(entities),
        window_start=window_start,
        window_end=window_end,
    )


def digest_from_stateless_job(
    *,
    entity_ids: list[str],
    results: dict[str, dict[str, Any]],
    errors: dict[str, str],
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> BriefDigest:
    """Build a digest from an in-memory stateless job registry entry."""
    entities: list[DigestEntity] = []
    for entity_id in entity_ids:
        if entity_id in errors:
            entities.append(
                DigestEntity(
                    entity_id=entity_id,
                    entity_name=entity_id,
                    error=errors[entity_id],
                )
            )
            continue

        report = results.get(entity_id) or {}
        name = str(report.get("entity_name") or entity_id)
        bullets_raw = report.get("bullets") or []
        bullets: list[DigestBullet] = []
        for item in bullets_raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            cites = _parse_citations(item.get("citations"))
            bullets.append(DigestBullet(text=text, citations=cites))

        entities.append(
            DigestEntity(
                entity_id=entity_id,
                entity_name=name,
                bullets=tuple(bullets),
            )
        )

    return BriefDigest(
        entities=tuple(entities),
        window_start=window_start,
        window_end=window_end,
    )

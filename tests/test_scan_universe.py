"""Tests for scan universe support in the REST endpoint and UI route."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from bigdata_briefs.api.routes.scan import (
    _parse_dates,
    _start_one_scan,
    build_scan_windows,
    resolve_scan_start,
)
from bigdata_briefs.orchestration.models import (
    SQLEntityOrchestrationState,
    SQLEntityPipelineRunLog,
    SQLUIScanRun,
)

FAR_SCAN_END = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    return eng


def _make_executor():
    ex = MagicMock()
    ex.submit = MagicMock()
    return ex


def _make_clients():
    return MagicMock(), MagicMock(), MagicMock()


# ── _parse_dates ──────────────────────────────────────────────────────────────


def test_parse_dates_valid_single():
    start, end = _parse_dates("2026-01-01", None)
    assert start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end.tzinfo is not None  # defaults to now


def test_parse_dates_valid_with_end():
    start, end = _parse_dates("2026-01-01", "2026-01-10")
    assert start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 10, 23, 59, 59, tzinfo=timezone.utc)


def test_parse_dates_invalid_start_raises():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _parse_dates("not-a-date", None)
    assert exc.value.status_code == 422


def test_parse_dates_invalid_end_raises():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _parse_dates("2026-01-01", "bad")


# ── _start_one_scan ───────────────────────────────────────────────────────────


def test_start_one_scan_creates_db_row_and_submits(engine):
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, 23, 59, 59, tzinfo=timezone.utc)

    resp = _start_one_scan(
        "E1", start, end, engine, executor, rate_limiter, connection_sem, http_client
    )

    assert resp is not None
    assert resp.entity_id == "E1"
    assert resp.windows_total == 3
    executor.submit.assert_called_once()

    with Session(engine) as s:
        row = s.get(SQLUIScanRun, resp.scan_id)
    assert row is not None
    assert row.status == "running"
    assert row.windows_total == 3


def test_start_one_scan_returns_none_when_no_windows(engine):
    """If entity is already up to date, _start_one_scan returns None."""
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    # Set last_window_end to after the requested range
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(
            entity_id="E2",
            last_window_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ))
        s.commit()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 15, 23, 59, 59, tzinfo=timezone.utc)

    resp = _start_one_scan(
        "E2", start, end, engine, executor, rate_limiter, connection_sem, http_client
    )

    assert resp is None
    executor.submit.assert_not_called()


def test_start_one_scan_uses_entity_name_from_db(engine):
    """Entity name is read from SQLEntityOrchestrationState if available."""
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E3", kg_name="Acme Corp."))
        s.commit()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 23, 59, 59, tzinfo=timezone.utc)
    resp = _start_one_scan(
        "E3", start, end, engine, executor, rate_limiter, connection_sem, http_client
    )

    with Session(engine) as s:
        row = s.get(SQLUIScanRun, resp.scan_id)
    assert row.entity_name == "Acme Corp."


# ── REST ScanRequest validation ───────────────────────────────────────────────


def test_scan_request_requires_entity_or_universe(engine):
    """start_scan raises 422 when neither entity_id nor universe is provided."""
    from fastapi import HTTPException
    from bigdata_briefs.api.routes.scan import start_scan, ScanRequest

    body = ScanRequest(start_date="2026-01-01")
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    with patch("bigdata_briefs.api.routes.scan.get_engine", return_value=engine):
        with pytest.raises(HTTPException) as exc:
            start_scan(body, executor, rate_limiter, connection_sem, http_client)
    assert exc.value.status_code == 422


def test_scan_request_rejects_both_entity_and_universe(engine):
    from fastapi import HTTPException
    from bigdata_briefs.api.routes.scan import start_scan, ScanRequest

    body = ScanRequest(entity_id="E1", universe="dow_30", start_date="2026-01-01")
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    with patch("bigdata_briefs.api.routes.scan.get_engine", return_value=engine):
        with pytest.raises(HTTPException) as exc:
            start_scan(body, executor, rate_limiter, connection_sem, http_client)
    assert exc.value.status_code == 422


def test_scan_request_rejects_unknown_universe(engine):
    from fastapi import HTTPException
    from bigdata_briefs.api.routes.scan import start_scan, ScanRequest

    body = ScanRequest(universe="nonexistent_xyz", start_date="2026-01-01")
    executor = _make_executor()
    rate_limiter, connection_sem, http_client = _make_clients()

    with patch("bigdata_briefs.api.routes.scan.get_engine", return_value=engine):
        with pytest.raises(HTTPException) as exc:
            start_scan(body, executor, rate_limiter, connection_sem, http_client)
    assert exc.value.status_code == 404


# ── build_scan_windows ────────────────────────────────────────────────────────


def test_build_scan_windows_single_day():
    start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 15, 23, 59, 59, tzinfo=timezone.utc)
    windows = build_scan_windows(start, end)
    assert len(windows) == 1
    assert windows[0][0] == start
    assert windows[0][1] == end


def test_build_scan_windows_three_days():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, 23, 59, 59, tzinfo=timezone.utc)
    windows = build_scan_windows(start, end)
    assert len(windows) == 3
    # Each window covers one calendar day
    assert windows[0][0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert windows[0][1] == datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    assert windows[1][0] == datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    assert windows[2][1] == datetime(2026, 1, 3, 23, 59, 59, tzinfo=timezone.utc)


def test_build_scan_windows_partial_last_day():
    """Last window ends at `end` even if it's mid-day."""
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
    windows = build_scan_windows(start, end)
    assert len(windows) == 2
    assert windows[1][1] == end  # clipped at end, not at 23:59:59


def test_build_scan_windows_empty_when_start_equals_end():
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    windows = build_scan_windows(start, start)
    assert windows == []


def test_build_scan_windows_live_scan_clips_at_end():
    """When end is mid-day (e.g. now), windows stop at that instant, not at 23:59:59."""
    now = datetime(2026, 1, 5, 10, 30, 0, tzinfo=timezone.utc)
    start = datetime(2026, 1, 4, 0, 0, 0, tzinfo=timezone.utc)
    windows = build_scan_windows(start, now)
    assert len(windows) == 2
    assert windows[0][1] == datetime(2026, 1, 4, 23, 59, 59, tzinfo=timezone.utc)
    assert windows[1][0] == datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    assert windows[1][1] == now  # clipped at "now", not at 23:59:59


# ── resolve_scan_start ────────────────────────────────────────────────────────


def test_resolve_scan_start_no_prior_runs(engine):
    """Without prior runs, requested_start is used as-is."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = resolve_scan_start(engine, "NEW_ENTITY", start, FAR_SCAN_END)
    assert result == start


def test_resolve_scan_start_resumes_from_last_window_end(engine):
    """With a prior run ending after requested_start, resume from last_window_end."""
    last_end = datetime(2026, 1, 10, 14, 30, 0, tzinfo=timezone.utc)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E5", last_window_end=last_end))
        s.commit()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = resolve_scan_start(engine, "E5", start, FAR_SCAN_END)
    assert result == last_end


def test_resolve_scan_start_uses_requested_when_prior_is_older(engine):
    """If last_window_end is before requested_start, use requested_start."""
    last_end = datetime(2025, 12, 1, tzinfo=timezone.utc)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E6", last_window_end=last_end))
        s.commit()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = resolve_scan_start(engine, "E6", start, FAR_SCAN_END)
    assert result == start


def test_resolve_scan_start_same_day_stuck_uses_process_completed(engine):
    """When last_window_end is same UTC day as scan_end but >= scan_end, use completion time."""
    rid = uuid.uuid4()
    day = datetime(2026, 4, 12, tzinfo=timezone.utc)
    last_end = datetime(2026, 4, 12, 23, 59, 59, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 12, 13, 0, 0, tzinfo=timezone.utc)
    with Session(engine) as s:
        s.add(SQLEntityOrchestrationState(entity_id="E7", last_window_end=last_end))
        s.add(SQLEntityPipelineRunLog(
            run_id=rid,
            entity_id="E7",
            report_window_start=datetime(2026, 4, 12, 0, 0, 0, tzinfo=timezone.utc),
            report_window_end=last_end,
            process_started_at_utc=datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc),
            process_completed_at_utc=completed,
            status="succeeded",
        ))
        s.commit()

    requested = datetime(2026, 4, 12, 0, 0, 0, tzinfo=timezone.utc)
    scan_end = datetime(2026, 4, 12, 15, 0, 0, tzinfo=timezone.utc)
    result = resolve_scan_start(engine, "E7", requested, scan_end)
    assert result == completed

"""Unit tests for ``build_report_dates_for_entity_run``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bigdata_briefs.orchestration.windows import (
    MAX_LOOKBACK_HOURS,
    WindowEndNotAfterStartError,
    WindowMode,
    build_report_dates_for_entity_run,
    utc_midnight,
)


def test_first_run_uses_utc_midnight() -> None:
    now = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
    rd = build_report_dates_for_entity_run(now=now, last_window_end=None)
    assert rd.start == utc_midnight(now.date())
    assert rd.end == now


def test_incremental_within_24h_uses_last_window_end() -> None:
    """Last run 6 h ago — well within 24 h cap, start = last_window_end."""
    now = datetime(2025, 3, 11, 15, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=6)  # 09:00 same day
    rd = build_report_dates_for_entity_run(now=now, last_window_end=last)
    assert rd.start == last
    assert rd.end == now


def test_incremental_last_run_older_than_cap_uses_floor() -> None:
    """ROLLING_24H: last run at 13:00 yesterday, now 15:00 → gap = 26 h > 24 h cap.
    now - 24h = yesterday 15:00 > last (13:00) → start = yesterday 15:00."""
    now = datetime(2025, 3, 11, 15, 0, tzinfo=timezone.utc)
    last = datetime(2025, 3, 10, 13, 0, tzinfo=timezone.utc)  # 26 h ago
    expected_start = now - timedelta(hours=MAX_LOOKBACK_HOURS)
    rd = build_report_dates_for_entity_run(now=now, last_window_end=last, window_mode=WindowMode.ROLLING_24H)
    assert rd.start == expected_start
    assert rd.end == now


def test_incremental_last_run_within_cap_but_recent() -> None:
    """ROLLING_24H: last run at 17:00 yesterday, now 15:00 today → gap = 22 h < 24 h cap.
    now - 24h = yesterday 15:00 < last (17:00) → start = yesterday 17:00."""
    now = datetime(2025, 3, 11, 15, 0, tzinfo=timezone.utc)
    last = datetime(2025, 3, 10, 17, 0, tzinfo=timezone.utc)  # 22 h ago
    rd = build_report_dates_for_entity_run(now=now, last_window_end=last, window_mode=WindowMode.ROLLING_24H)
    assert rd.start == last
    assert rd.end == now


def test_naive_last_window_end_treated_as_utc() -> None:
    """Naive datetime for last_window_end should be normalized to UTC."""
    now = datetime(2025, 3, 11, 12, 0, tzinfo=timezone.utc)
    last = datetime(2025, 3, 11, 10, 0)  # naive
    rd = build_report_dates_for_entity_run(now=now, last_window_end=last)
    assert rd.start == last.replace(tzinfo=timezone.utc)
    assert rd.end == now


def test_raises_when_window_empty() -> None:
    t = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(WindowEndNotAfterStartError):
        build_report_dates_for_entity_run(now=t, last_window_end=t)

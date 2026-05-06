"""Incremental report window builder for entity orchestration (UTC, half-open)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

from bigdata_briefs.models import ReportDates

MAX_LOOKBACK_HOURS = 24


class WindowMode(str, Enum):
    """Controls how the start of the search window is determined.

    daily (default)
        Starts at UTC midnight of the current calendar day, unless the company
        was already run earlier today — in that case starts from where that run
        ended, to avoid reprocessing the same content twice.
        A run from a previous calendar day never influences today's window start.

    continuous
        Starts exactly where the previous run's window ended, with no cap.
        Guarantees a gap-free timeline across consecutive runs.
        Falls back to UTC midnight of today on the very first run.

    rolling_24h
        Always covers the 24 hours preceding the current run, capped at
        ``last_window_end`` to avoid re-querying already-searched content.
        Equivalent to ``start = max(last_window_end, now - 24h)``.
        Falls back to UTC midnight of today on the very first run.

    daily_update
        Covers at most the 24 hours preceding ``end``.
        If a previous run exists whose ``last_window_end`` falls within that
        24-hour window, starts from there instead (avoiding redundant reprocessing).
        If no previous run exists, covers the full 24 hours (first-run friendly).
        Equivalent to ``start = max(last_window_end, end - 24h)``, with
        ``start = end - 24h`` as the fallback when there is no history.
    """

    DAILY = "daily"
    CONTINUOUS = "continuous"
    ROLLING_24H = "rolling_24h"
    DAILY_UPDATE = "daily_update"


class WindowEndNotAfterStartError(ValueError):
    """Raised when ``now`` is not strictly after the window start (no-op run)."""


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def build_report_dates_for_entity_run(
    *,
    now: datetime,
    last_window_end: datetime | None,
    window_mode: WindowMode = WindowMode.DAILY,
) -> ReportDates:
    """
    Build ``ReportDates`` with half-open semantics ``[start, end)``.

    ``end`` is always ``now`` (UTC-normalised).  ``start`` depends on
    ``window_mode`` — see ``WindowMode`` for full semantics.

    Raises:
        WindowEndNotAfterStartError: if ``end <= start`` after normalisation.
    """
    end = _ensure_utc(now)

    if window_mode == WindowMode.DAILY:
        today_midnight = utc_midnight(end.date())
        if last_window_end is not None and _ensure_utc(last_window_end) > today_midnight:
            # Already run today: pick up from where the last run ended.
            start = _ensure_utc(last_window_end)
        else:
            # First run of the day (or ever): start at midnight of today.
            start = today_midnight

    elif window_mode == WindowMode.CONTINUOUS:
        # Pick up exactly where the last run ended; no cap.
        if last_window_end is None:
            start = utc_midnight(end.date())
        else:
            start = _ensure_utc(last_window_end)

    elif window_mode == WindowMode.ROLLING_24H:
        # Cover the last 24 hours, capped at last_window_end to avoid overlap.
        if last_window_end is None:
            start = utc_midnight(end.date())
        else:
            floor = end - timedelta(hours=MAX_LOOKBACK_HOURS)
            start = max(_ensure_utc(last_window_end), floor)

    else:  # DAILY_UPDATE
        # At most 24h back from end. Resume from last_window_end if it falls
        # within that window; otherwise cover the full 24h (first-run friendly).
        floor = end - timedelta(hours=MAX_LOOKBACK_HOURS)
        if last_window_end is None:
            start = floor
        else:
            start = max(_ensure_utc(last_window_end), floor)

    if end <= start:
        raise WindowEndNotAfterStartError(
            f"Report window empty or inverted: start={start!r} end={end!r}"
        )
    return ReportDates(start=start, end=end)

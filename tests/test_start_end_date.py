"""Tests for ReportDates / date filter prompt text."""

from datetime import datetime

from bigdata_briefs.models import ReportDates


def test_date_filter_instructions_single_calendar_day() -> None:
    """Single-day report: use 'Today is …' wording, not a redundant date range."""
    rd = ReportDates(
        start=datetime(2026, 1, 22, 0, 0, 0),
        end=datetime(2026, 1, 23, 0, 0, 0),
    )
    text = rd.get_date_filter_instructions()
    assert "Today is January 22, 2026" in text
    assert "to January 22, 2026" not in text
    assert "The reporting period is" not in text
    assert "no explicit date" in text
    assert "NEW update" in text


def test_date_filter_instructions_multi_day_range() -> None:
    rd = ReportDates(
        start=datetime(2023, 1, 1, 0, 0, 0),
        end=datetime(2023, 1, 15, 0, 0, 0),
    )
    text = rd.get_date_filter_instructions()
    assert "The reporting period is January 01, 2023 to January 14, 2023" in text
    assert "Today is" not in text
    assert "no explicit date" in text

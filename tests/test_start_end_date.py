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


# ── temporal reference warning ────────────────────────────────────────────────


def test_date_filter_instructions_single_day_contains_temporal_warning() -> None:
    """Single-day: date filter must include the temporal-reference-only warning."""
    rd = ReportDates(
        start=datetime(2026, 4, 22, 0, 0, 0),
        end=datetime(2026, 4, 23, 0, 0, 0),
    )
    text = rd.get_date_filter_instructions()
    assert "temporal reference" in text
    assert "do not" in text.lower()
    assert "source" in text.lower()


def test_date_filter_instructions_multi_day_contains_temporal_warning() -> None:
    """Multi-day: same temporal-reference-only warning must appear."""
    rd = ReportDates(
        start=datetime(2026, 4, 1, 0, 0, 0),
        end=datetime(2026, 4, 10, 0, 0, 0),
    )
    text = rd.get_date_filter_instructions()
    assert "temporal reference" in text
    assert "source" in text.lower()


def test_date_phrase_single_day_contains_temporal_suffix() -> None:
    """get_date_phrase_for_prompt must append the temporal-reference disclaimer."""
    rd = ReportDates(
        start=datetime(2026, 4, 22, 0, 0, 0),
        end=datetime(2026, 4, 23, 0, 0, 0),
    )
    phrase = rd.get_date_phrase_for_prompt()
    assert "Today is" in phrase
    assert "temporal reference only" in phrase
    assert "source excerpt" in phrase


def test_date_phrase_multi_day_contains_temporal_suffix() -> None:
    rd = ReportDates(
        start=datetime(2026, 4, 1, 0, 0, 0),
        end=datetime(2026, 4, 10, 0, 0, 0),
    )
    phrase = rd.get_date_phrase_for_prompt()
    assert "We are analyzing the period" in phrase
    assert "temporal reference only" in phrase
    assert "source excerpt" in phrase


def test_date_phrase_does_not_change_date_content() -> None:
    """The date itself must still be present and correct — only the suffix is new."""
    rd = ReportDates(
        start=datetime(2026, 1, 15, 0, 0, 0),
        end=datetime(2026, 1, 16, 0, 0, 0),
    )
    phrase = rd.get_date_phrase_for_prompt()
    assert "January 15, 2026" in phrase

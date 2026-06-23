"""Tests for map_earnings_calls_on_calendar_day (Brief front-page earnings flag)."""

import json
from datetime import date
from unittest.mock import patch

from bigdata_briefs.orchestration.earnings_calendar_cache import earnings_flags_for_calendar_day
from bigdata_briefs.temporal import map_earnings_calls_on_calendar_day


def test_flags_true_when_event_on_same_utc_day():
    fake = {
        "results": {
            "E1": [
                {
                    "event_datetime": "2026-04-27T20:00:00+00:00",
                    "title": "Q3 FY2026 Earnings",
                    "fiscal_year": 2026,
                    "fiscal_period": "Q3",
                }
            ],
            "E2": [],
        }
    }
    with patch("bigdata_briefs.temporal._events_calendar_query", return_value=fake):
        out = map_earnings_calls_on_calendar_day(date(2026, 4, 27), ["E1", "E2"], api_key="x")
    assert out["E1"]["on_date"] is True
    assert out["E1"]["session_title"] == "Q3 FY2026 Earnings"
    assert out["E2"]["on_date"] is False


def test_title_fallback_fiscal_fields():
    fake = {
        "results": {
            "E1": [
                {
                    "event_datetime": "2026-04-27T14:00:00+00:00",
                    "fiscal_year": 2025,
                    "fiscal_period": "Q4",
                }
            ],
        }
    }
    with patch("bigdata_briefs.temporal._events_calendar_query", return_value=fake):
        out = map_earnings_calls_on_calendar_day(date(2026, 4, 27), ["E1"], api_key="x")
    assert out["E1"]["on_date"] is True
    assert out["E1"]["session_title"] == "Q4 2025"


def test_empty_api_key_returns_false():
    out = map_earnings_calls_on_calendar_day(date(2026, 4, 27), ["E1"], api_key="")
    assert out["E1"]["on_date"] is False


def test_earnings_flags_from_cached_json():
    events = [
        {
            "event_datetime": "2026-04-27T16:00:00+00:00",
            "title": "Q2 FY2026",
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
        }
    ]
    on, title = earnings_flags_for_calendar_day(json.dumps(events), date(2026, 4, 27))
    assert on is True
    assert title == "Q2 FY2026"

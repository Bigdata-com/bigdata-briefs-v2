"""Tests for optional Resend email digests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from bigdata_briefs.notifications.assemble import digest_from_stateless_job
from bigdata_briefs.notifications.email_digest import (
    BriefDigest,
    DigestBullet,
    DigestCitation,
    DigestEntity,
    digest_subject,
)
from bigdata_briefs.notifications.email_html import build_email_html
from bigdata_briefs.notifications.resend_client import maybe_send_brief_email


def _sample_digest() -> BriefDigest:
    return BriefDigest(
        entities=(
            DigestEntity(
                entity_id="D8442A",
                entity_name="Apple Inc.",
                bullets=(
                    DigestBullet(
                        text="Apple raised FY guidance.",
                        citations=(
                            DigestCitation(
                                source_name="Reuters",
                                headline="Apple lifts outlook",
                                url="https://example.com/a",
                            ),
                        ),
                    ),
                ),
            ),
            DigestEntity(
                entity_id="E0207A",
                entity_name="SS&C Technologies",
                bullets=(),
            ),
            DigestEntity(
                entity_id="BAD001",
                entity_name="Failed Co",
                error="timeout",
            ),
        ),
        window_start=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )


def test_build_email_html_includes_empty_and_failed_companies() -> None:
    html = build_email_html(_sample_digest())

    assert "Apple Inc." in html
    assert "Apple raised FY guidance." in html
    assert "https://example.com/a" in html
    assert "No material developments." in html
    assert "Run failed: timeout" in html
    assert "1 with developments" in html
    assert "1 empty" in html
    assert "1 failed" in html


def test_digest_subject_multi_and_single() -> None:
    multi = _sample_digest()
    assert digest_subject(multi) == "Briefs ready — 2026-07-15 (3 companies)"

    single = BriefDigest(
        entities=(
            DigestEntity(entity_id="D8442A", entity_name="Apple Inc.", bullets=()),
        ),
        window_end=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    assert digest_subject(single) == "Brief ready — Apple Inc. (2026-07-15)"


def test_digest_from_stateless_job_includes_empty_entities() -> None:
    digest = digest_from_stateless_job(
        entity_ids=["A", "B", "C"],
        results={
            "A": {
                "entity_name": "Alpha",
                "bullets": [
                    {
                        "text": "Alpha grew revenue.",
                        "citations": [
                            {
                                "source_name": "WSJ",
                                "headline": "Alpha beats",
                                "url": "https://ex.com/1",
                            }
                        ],
                    }
                ],
            },
            "B": {"entity_name": "Beta", "bullets": []},
        },
        errors={"C": "boom"},
        window_start=datetime(2026, 7, 14, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    assert len(digest.entities) == 3
    assert digest.entities[0].bullets[0].text == "Alpha grew revenue."
    assert digest.entities[1].bullets == ()
    assert digest.entities[2].error == "boom"
    assert digest.counts() == (1, 1, 1)


def test_maybe_send_brief_email_noop_when_disabled() -> None:
    with patch("bigdata_briefs.notifications.resend_client.settings") as mock_settings:
        mock_settings.BRIEFS_EMAIL_ENABLED = False
        mock_settings.RESEND_API_KEY = "re_test"
        mock_settings.BRIEFS_EMAIL_TO = "a@b.com"
        mock_settings.BRIEFS_EMAIL_FROM = "onboarding@resend.dev"
        with patch.dict("sys.modules", {"resend": MagicMock()}) as modules:
            maybe_send_brief_email(_sample_digest())
            assert modules["resend"].Emails.send.call_count == 0


def test_maybe_send_brief_email_noop_when_missing_to() -> None:
    with patch("bigdata_briefs.notifications.resend_client.settings") as mock_settings:
        mock_settings.BRIEFS_EMAIL_ENABLED = True
        mock_settings.RESEND_API_KEY = "re_test"
        mock_settings.BRIEFS_EMAIL_TO = ""
        mock_settings.BRIEFS_EMAIL_FROM = "onboarding@resend.dev"
        fake_resend = MagicMock()
        with patch.dict("sys.modules", {"resend": fake_resend}):
            maybe_send_brief_email(_sample_digest())
            assert fake_resend.Emails.send.call_count == 0


def test_maybe_send_brief_email_calls_resend_when_enabled() -> None:
    with patch("bigdata_briefs.notifications.resend_client.settings") as mock_settings:
        mock_settings.BRIEFS_EMAIL_ENABLED = True
        mock_settings.RESEND_API_KEY = "re_test"
        mock_settings.BRIEFS_EMAIL_TO = "fgomez@example.com"
        mock_settings.BRIEFS_EMAIL_FROM = "onboarding@resend.dev"
        fake_resend = MagicMock()
        fake_resend.Emails.send.return_value = {"id": "email_1"}
        with patch.dict("sys.modules", {"resend": fake_resend}):
            maybe_send_brief_email(_sample_digest())

        fake_resend.Emails.send.assert_called_once()
        payload = fake_resend.Emails.send.call_args.args[0]
        assert payload["to"] == ["fgomez@example.com"]
        assert payload["from"] == "onboarding@resend.dev"
        assert "Briefs ready" in payload["subject"]
        assert "Apple raised FY guidance." in payload["html"]
        assert fake_resend.api_key == "re_test"

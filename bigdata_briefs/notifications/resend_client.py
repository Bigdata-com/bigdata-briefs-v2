"""Lazy Resend client for optional brief email digests."""

from __future__ import annotations

from bigdata_briefs import logger
from bigdata_briefs.notifications.email_digest import BriefDigest, digest_subject
from bigdata_briefs.notifications.email_html import build_email_html
from bigdata_briefs.settings import settings


def email_notifications_configured() -> bool:
    """Return True when the explicit enable flag and required credentials are set."""
    return bool(
        settings.BRIEFS_EMAIL_ENABLED
        and settings.RESEND_API_KEY.strip()
        and settings.BRIEFS_EMAIL_TO.strip()
    )


def maybe_send_brief_email(digest: BriefDigest) -> None:
    """Send a digest email when email notifications are enabled.

    Best-effort: never raises into the pipeline. No-ops when disabled or misconfigured.
    Lazily imports ``resend`` only when a send is attempted.
    """
    if not settings.BRIEFS_EMAIL_ENABLED:
        return

    api_key = settings.RESEND_API_KEY.strip()
    to_addr = settings.BRIEFS_EMAIL_TO.strip()
    from_addr = settings.BRIEFS_EMAIL_FROM.strip() or "onboarding@resend.dev"

    if not api_key or not to_addr:
        logger.warning(
            "brief_email_skipped_missing_config",
            has_api_key=bool(api_key),
            has_to=bool(to_addr),
        )
        return

    if not digest.entities:
        logger.info("brief_email_skipped_empty_digest")
        return

    try:
        import resend
    except ImportError:
        logger.error(
            "brief_email_resend_not_installed",
            hint="Install with: uv sync --extra email",
        )
        return

    subject = digest_subject(digest)
    html_body = build_email_html(digest)

    try:
        resend.api_key = api_key
        result = resend.Emails.send(
            {
                "from": from_addr,
                "to": [to_addr],
                "subject": subject,
                "html": html_body,
            }
        )
        logger.info(
            "brief_email_sent",
            to=to_addr,
            subject=subject,
            entities=len(digest.entities),
            result=str(result),
        )
    except Exception:
        logger.exception("brief_email_send_failed", to=to_addr, subject=subject)

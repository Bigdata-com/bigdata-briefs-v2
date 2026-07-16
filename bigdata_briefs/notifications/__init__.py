"""Optional post-run notifications (email digests)."""

from bigdata_briefs.notifications.email_digest import (
    BriefDigest,
    DigestBullet,
    DigestCitation,
    DigestEntity,
)
from bigdata_briefs.notifications.resend_client import maybe_send_brief_email

__all__ = [
    "BriefDigest",
    "DigestBullet",
    "DigestCitation",
    "DigestEntity",
    "maybe_send_brief_email",
]

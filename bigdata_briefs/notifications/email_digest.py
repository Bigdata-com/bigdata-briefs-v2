"""Structured payload for brief email digests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DigestCitation:
    source_name: str = ""
    headline: str = ""
    url: str | None = None


@dataclass(frozen=True)
class DigestBullet:
    text: str
    citations: tuple[DigestCitation, ...] = ()


@dataclass(frozen=True)
class DigestEntity:
    entity_id: str
    entity_name: str
    bullets: tuple[DigestBullet, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class BriefDigest:
    """One email digest covering one or more entities from a completed run."""

    entities: tuple[DigestEntity, ...]
    window_start: datetime | None = None
    window_end: datetime | None = None
    title_hint: str = ""

    def counts(self) -> tuple[int, int, int]:
        """Return (with_developments, empty, failed)."""
        with_dev = 0
        empty = 0
        failed = 0
        for entity in self.entities:
            if entity.error:
                failed += 1
            elif entity.bullets:
                with_dev += 1
            else:
                empty += 1
        return with_dev, empty, failed


def digest_subject(digest: BriefDigest) -> str:
    """Build a short email subject line."""
    date_label = ""
    if digest.window_end is not None:
        date_label = digest.window_end.date().isoformat()
    elif digest.window_start is not None:
        date_label = digest.window_start.date().isoformat()

    if len(digest.entities) == 1:
        name = digest.entities[0].entity_name or digest.entities[0].entity_id
        if date_label:
            return f"Brief ready — {name} ({date_label})"
        return f"Brief ready — {name}"

    n = len(digest.entities)
    if date_label:
        return f"Briefs ready — {date_label} ({n} companies)"
    return f"Briefs ready — {n} companies"

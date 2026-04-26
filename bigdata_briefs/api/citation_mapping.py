"""Map persisted citation JSON dicts to API CitationDetail models."""

from __future__ import annotations

from typing import Any

from bigdata_briefs.api.schemas import CitationDetail


def stored_citation_dict_to_detail(c: dict[str, Any]) -> CitationDetail:
    """Build API CitationDetail from a dict in generated_bullet_points.citations JSON."""
    return CitationDetail(
        id=str(c.get("id") or ""),
        headline=str(c.get("headline") or ""),
        text=str(c.get("text") or ""),
        source_name=str(c.get("source_name") or ""),
    )

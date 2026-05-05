"""API authentication — open access, no API key required."""

from __future__ import annotations


async def require_api_key() -> None:
    """No-op dependency kept for route compatibility."""
    return

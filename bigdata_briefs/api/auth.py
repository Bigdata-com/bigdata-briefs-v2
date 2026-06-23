"""API authentication — API key required when PIPELINE_API_KEY is set."""

from __future__ import annotations

from fastapi import Header, HTTPException, status
from bigdata_briefs.settings import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Validate X-Api-Key header when PIPELINE_API_KEY is configured.

    If PIPELINE_API_KEY is not set (empty string), auth is skipped — safe for
    local development. On production set it via env/secrets.
    """
    expected = getattr(settings, "PIPELINE_API_KEY", "")
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )

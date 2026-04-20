"""API key authentication dependency."""

from __future__ import annotations

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from bigdata_briefs.settings import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Validate X-API-Key header against PIPELINE_API_KEY.

    If PIPELINE_API_KEY is empty, authentication is skipped (open access).
    """
    configured = settings.PIPELINE_API_KEY
    if not configured:
        return
    if api_key != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

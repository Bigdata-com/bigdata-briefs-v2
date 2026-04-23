"""API key authentication dependency."""

from __future__ import annotations

from fastapi import HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader

from bigdata_briefs.settings import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key_header: str | None = Security(_api_key_header),
    api_key: str | None = Query(default=None, alias="api_key", include_in_schema=False),
) -> None:
    """Validate API key from X-API-Key header or ?api_key= query parameter.

    If PIPELINE_API_KEY is empty, authentication is skipped (open access).
    The query param variant allows opening HTML endpoints directly in the browser.
    """
    configured = settings.PIPELINE_API_KEY
    if not configured:
        return
    provided = api_key_header or api_key
    if provided != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

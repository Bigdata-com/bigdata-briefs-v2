"""Universe endpoints — named lists of entity IDs.

A universe is simply a named collection of entity IDs that can be passed
directly to batch endpoints.  Use GET /universes to list all available
universes; use GET /universes/{name} to retrieve the entity IDs for a
specific one.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bigdata_briefs.api.schemas import UniverseListResponse, UniverseResponse

router = APIRouter(tags=["universes"])

# ---------------------------------------------------------------------------
# Universe registry
# Add entity IDs to the lists below to populate a universe.
# ---------------------------------------------------------------------------

_UNIVERSES: dict[str, list[str]] = {
    "dow_30": [],
    "eurostoxx_50": [],
}


@router.get(
    "/universes",
    response_model=UniverseListResponse,
    summary="List all available universes",
    description=(
        "Returns the names of all registered company universes and the number "
        "of entities in each one."
    ),
)
def list_universes() -> UniverseListResponse:
    return UniverseListResponse(
        universes=[
            UniverseResponse(name=name, entity_ids=ids, total=len(ids))
            for name, ids in _UNIVERSES.items()
        ]
    )


@router.get(
    "/universes/{name}",
    response_model=UniverseResponse,
    summary="Get entity IDs for a universe",
    description="Returns the list of entity IDs belonging to the requested universe.",
)
def get_universe(name: str) -> UniverseResponse:
    ids = _UNIVERSES.get(name)
    if ids is None:
        raise HTTPException(
            status_code=404,
            detail=f"Universe '{name}' not found. Available: {list(_UNIVERSES)}",
        )
    return UniverseResponse(name=name, entity_ids=ids, total=len(ids))

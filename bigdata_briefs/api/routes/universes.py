"""Universe endpoints — named lists of entity IDs.

A universe is simply a named collection of entity IDs that can be passed
directly to batch endpoints.  Use GET /universes to list all available
universes; use GET /universes/{name} to retrieve the entity IDs for a
specific one.

Universe definitions are loaded at startup from CSV files in
``bigdata_briefs/data/universes/``. Each CSV must have ``id`` and ``name``
columns. Adding a new CSV file automatically registers a new universe.
"""

from __future__ import annotations

import csv
from pathlib import Path

from fastapi import APIRouter, HTTPException

from bigdata_briefs.api.schemas import UniverseListResponse, UniverseResponse

router = APIRouter(tags=["universes"])

_UNIVERSES_DIR = Path(__file__).parent.parent.parent / "data" / "universes"


def _load_universes() -> dict[str, list[str]]:
    """Load all universes from CSV files in the universes data directory."""
    universes: dict[str, list[str]] = {}
    for csv_path in sorted(_UNIVERSES_DIR.glob("*.csv")):
        name = csv_path.stem
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            universes[name] = [row["id"] for row in reader if row.get("id")]
    return universes


_UNIVERSES: dict[str, list[str]] = _load_universes()


def _get_my_portfolio_ids() -> list[str]:
    """Fetch live entity_ids from SQLUserPortfolio at request time."""
    try:
        from bigdata_briefs.api.dependencies import get_engine
        from bigdata_briefs.orchestration.models import SQLUserPortfolio
        from sqlmodel import Session, select

        engine = get_engine()
        with Session(engine) as session:
            rows = session.exec(select(SQLUserPortfolio).order_by(SQLUserPortfolio.added_at)).all()
        return [r.entity_id for r in rows]
    except Exception:
        return []


@router.get(
    "/universes",
    response_model=UniverseListResponse,
    summary="List all available universes",
    description=(
        "Returns the names of all registered company universes and the number "
        "of entities in each one. Universes are loaded from CSV files in "
        "``bigdata_briefs/data/universes/``. Also includes a live ``my_portfolio`` universe."
    ),
)
def list_universes() -> UniverseListResponse:
    portfolio_ids = _get_my_portfolio_ids()
    universes = [
        UniverseResponse(name=name, entity_ids=ids, total=len(ids))
        for name, ids in _UNIVERSES.items()
    ]
    universes.append(UniverseResponse(name="my_portfolio", entity_ids=portfolio_ids, total=len(portfolio_ids)))
    return UniverseListResponse(universes=universes)


@router.get(
    "/universes/{name}",
    response_model=UniverseResponse,
    summary="Get entity IDs for a universe",
    description="Returns the list of entity IDs belonging to the requested universe.",
)
def get_universe(name: str) -> UniverseResponse:
    if name == "my_portfolio":
        ids = _get_my_portfolio_ids()
        return UniverseResponse(name="my_portfolio", entity_ids=ids, total=len(ids))
    ids = _UNIVERSES.get(name)
    if ids is None:
        raise HTTPException(
            status_code=404,
            detail=f"Universe '{name}' not found. Available: {list(_UNIVERSES) + ['my_portfolio']}",
        )
    return UniverseResponse(name=name, entity_ids=ids, total=len(ids))

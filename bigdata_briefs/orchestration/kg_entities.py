"""Knowledge Graph entities-by-id API (batch) and mapping to ``Entity``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

from bigdata_briefs.models import Entity

if TYPE_CHECKING:
    from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController


def ticker_from_listing_values(listing_values: list[str] | None) -> str | None:
    """First listing; symbol is substring after last ``:`` (v1 heuristic)."""
    if not listing_values:
        return None
    first = listing_values[0].strip()
    if ":" in first:
        return first.rsplit(":", 1)[-1].strip() or None
    return first or None


def _require_non_empty_str(rec: dict[str, Any], field: str, *, entity_id: str) -> str:
    raw = rec.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"KG record field {field!r} must be a non-empty string for id={entity_id!r}, got {raw!r}"
        )
    return raw.strip()


def kg_record_to_entity_dict(rec: dict[str, Any]) -> dict[str, Any]:
    """
    Shape ``Entity.from_api`` expects (``category`` + optional ``ticker``).

    Raises:
        ValueError: if the record is not a dict or required fields are missing/invalid.
        No implicit defaults for ``name`` or ``category``.
    """
    if not isinstance(rec, dict):
        raise ValueError(f"KG record must be a dict, got {type(rec).__name__}")

    eid_raw = rec.get("id")
    if not isinstance(eid_raw, str) or not eid_raw.strip():
        raise ValueError(f"KG record id must be a non-empty string, got {eid_raw!r}")
    eid = eid_raw.strip()

    name = _require_non_empty_str(rec, "name", entity_id=eid)
    category = _require_non_empty_str(rec, "category", entity_id=eid)

    if "listing_values" in rec and rec["listing_values"] is not None:
        listings = rec["listing_values"]
        if not isinstance(listings, list):
            raise ValueError(
                f"KG record listing_values must be a list or null for id={eid!r}, got {type(listings).__name__}"
            )
        for i, item in enumerate(listings):
            if not isinstance(item, str):
                raise ValueError(
                    f"KG record listing_values[{i}] must be str for id={eid!r}, got {type(item).__name__}"
                )
        ticker = ticker_from_listing_values(listings)
    else:
        ticker = ticker_from_listing_values(None)

    raw_ticker = rec.get("ticker")
    if raw_ticker is not None and not isinstance(raw_ticker, str):
        raise ValueError(
            f"KG record ticker must be a string or null for id={eid!r}, got {type(raw_ticker).__name__}"
        )
    resolved_ticker = ticker if ticker is not None else raw_ticker

    base = {
        k: v
        for k, v in rec.items()
        if k not in ("id", "name", "category", "ticker")
    }
    return {
        "id": eid,
        "name": name,
        "category": category,
        **base,
        "ticker": resolved_ticker,
    }


def entity_from_kg_record(rec: dict[str, Any]) -> Entity:
    return Entity.from_api(kg_record_to_entity_dict(rec))


def fetch_kg_entities_by_ids(
    entity_ids: list[str],
    *,
    api_key: str,
    base_url: str = "https://api.bigdata.com",
    timeout_seconds: int = 120,
    rate_limiter: "RequestsPerMinuteController | None" = None,
) -> dict[str, dict[str, Any]]:
    """
    POST ``/v1/knowledge-graph/entities/id`` with body ``{"values": [...]}``.

    When ``rate_limiter`` is provided, the KG POST is counted against the same
    process-global 450 QPM budget as every other Bigdata call. Without it,
    this endpoint would bypass rate-limiting — critical to fix since
    ``resolve_entity_for_run`` fires one of these per entity at the top of
    every run, and a parallel batch of 16 entities would otherwise send 16
    unmetered requests before the limiter sees anything.

    Returns:
        Mapping entity_id -> result object (same shape as API ``results[id]``).

    Raises:
        HTTPError, URLError, KeyError: on HTTP/network or malformed JSON.
    """
    if not entity_ids:
        return {}
    url = f"{base_url.rstrip('/')}/v1/knowledge-graph/entities/id"
    payload = json.dumps({"values": entity_ids}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if rate_limiter is not None:
        rate_limiter.acquire()
    with urlopen(req, timeout=timeout_seconds) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    results = body.get("results")
    if not isinstance(results, dict):
        raise ValueError("KG response missing 'results' object")
    out: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        r = results.get(eid)
        if isinstance(r, dict):
            out[eid] = r
    missing = [eid for eid in entity_ids if eid not in out]
    if missing:
        raise ValueError(
            "Knowledge Graph response missing or invalid (non-object) results for "
            f"entity_ids={missing!r}"
        )
    return out

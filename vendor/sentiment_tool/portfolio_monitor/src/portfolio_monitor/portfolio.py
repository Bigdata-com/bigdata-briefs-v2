"""Core portfolio logic — entity resolution, parallel data fetch, aggregation."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from portfolio_monitor._compat import ensure_sentiment_tool_on_path

ensure_sentiment_tool_on_path()

from openai import OpenAI  # noqa: E402
from sentiment_tool import (  # noqa: E402
    OPENAI_API_KEY,
    aggregate_to_docs,
    fetch_chunks,
    generate_narrative,
    get_signal_snapshot,
    resolve_company,
)

from portfolio_monitor.market_data import get_price_changes, get_quote

_ENTITY_ID_RE = re.compile(r"^[A-Z0-9]{6}$")

DEFAULT_MODEL = "gpt-4o-mini"
MAX_CHUNKS = 30
TOP_DOCS = 20


def _looks_like_entity_id(s: str) -> bool:
    return bool(_ENTITY_ID_RE.match(s.strip()))


def _slim_company(data: dict) -> dict:
    """Return only the fields needed in the MCP response — drop verbose internals."""
    keep = {
        "id", "name", "ticker", "sector", "industry",
        "signals", "market", "evidence", "narrative", "error",
    }
    return {k: v for k, v in data.items() if k in keep}


def resolve_entities(inputs: list[str], max_workers: int = 20) -> list[dict]:
    """Resolve a mixed list of entity IDs and company names to company dicts.

    Returns list of dicts with keys: id, name, description, industry, sector,
    country, type. Failed resolutions include an 'error' key and minimal fields.
    """
    def _resolve_one(raw: str) -> dict:
        raw = raw.strip()
        if not raw:
            return {"id": "", "name": raw, "error": "empty input"}
        if _looks_like_entity_id(raw):
            return {"id": raw, "name": raw, "description": "", "industry": "",
                    "sector": "", "country": "", "type": ""}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return resolve_company(raw)
            except Exception as exc:
                last_exc = exc
                # Retry on 429 with backoff; bail immediately on other errors
                if "429" in str(exc):
                    time.sleep(2 ** attempt)
                else:
                    break
        return {"id": "", "name": raw, "description": "", "industry": "",
                "sector": "", "country": "", "type": "", "error": str(last_exc)}

    with ThreadPoolExecutor(max_workers=min(max_workers, len(inputs) or 1)) as ex:
        futures = {ex.submit(_resolve_one, inp): inp for inp in inputs}
        results = []
        for fut in as_completed(futures):
            results.append(fut.result())

    # Preserve order matching the input list
    order = {raw.strip(): i for i, raw in enumerate(inputs)}
    results.sort(key=lambda d: order.get(d.get("name") or d.get("id", ""), 999))
    return results


def _fetch_company_data(
    company: dict,
    days: int,
    include_market: bool,
    include_chunks: bool,
) -> dict:
    """Run all I/O for a single company in parallel (4-way inner concurrency)."""
    entity_id = company["id"]
    now = datetime.now(timezone.utc)
    start_ts = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    futures: dict = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures["signals"] = ex.submit(get_signal_snapshot, entity_id)
        if include_chunks:
            futures["chunks"] = ex.submit(fetch_chunks, entity_id, start_ts, end_ts, MAX_CHUNKS, days)
        if include_market:
            futures["quote"] = ex.submit(get_quote, entity_id)
            futures["changes"] = ex.submit(get_price_changes, entity_id)

        collected: dict = {}
        for key, fut in futures.items():
            try:
                collected[key] = fut.result()
            except Exception as exc:
                collected[key] = {"error": str(exc)}

    result: dict = {**company}

    signals = collected.get("signals", {})
    result["signals"] = {
        "sentiment": signals.get("sentiment", {}),
        "media_attention": signals.get("media_attention", {}),
    }

    if include_chunks:
        chunk_data = collected.get("chunks", ([], 0, days))
        if isinstance(chunk_data, dict) and "error" in chunk_data:
            result["chunks_error"] = chunk_data["error"]
            result["_raw_chunks"] = []
            result["_chunk_days"] = days
        else:
            raw_chunks, _elapsed, actual_days = chunk_data
            result["_raw_chunks"] = raw_chunks
            result["_chunk_days"] = actual_days

    if include_market:
        quote = collected.get("quote", {})
        # Backfill company name/ticker from quote when entity ID was passed directly
        if quote.get("name") and result.get("name") == entity_id:
            result["name"] = quote["name"]
        if quote.get("target_identifier_id"):
            result["ticker"] = quote["target_identifier_id"]
        changes = collected.get("changes", {})
        price = quote.get("price")
        result["market"] = {
            "price_available": price is not None,
            "price":           price,
            "currency":        quote.get("currency"),
            "exchange":        quote.get("exchange"),
            "market_cap":      quote.get("market_cap"),
            "change_pct_1d":   quote.get("change_percentage"),
            "change_1d":       quote.get("change"),
            "day_high":        quote.get("day_high"),
            "day_low":         quote.get("day_low"),
            "year_high":       quote.get("year_high"),
            "year_low":        quote.get("year_low"),
            "volume":          quote.get("volume"),
            "prev_close":      quote.get("previous_close"),
            "timestamp":       quote.get("timestamp"),
            "change_pct_5d":   changes.get("5D"),
            "change_pct_1m":   changes.get("1M"),
            "change_pct_3m":   changes.get("3M"),
            "change_pct_6m":   changes.get("6M"),
            "change_pct_ytd":  changes.get("ytd"),
            "change_pct_1y":   changes.get("1Y"),
        }

    return result


def _sentiment_label(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score > 0.15:
        return "bullish"
    if score > 0.05:
        return "slightly_bullish"
    if score < -0.15:
        return "bearish"
    if score < -0.05:
        return "slightly_bearish"
    return "neutral"


def _build_portfolio_summary(company_results: list[dict]) -> dict:
    """Aggregate per-company results into a portfolio-level summary."""
    dist: dict[str, int] = {
        "bullish": 0, "slightly_bullish": 0, "neutral": 0,
        "slightly_bearish": 0, "bearish": 0,
    }
    sector_map: dict[str, list[float]] = {}
    sentiment_scores: list[float] = []
    changes_1d: list[float] = []

    for co in company_results:
        if co.get("error"):
            continue
        sent_current = (co.get("signals") or {}).get("sentiment", {}).get("current")
        label = _sentiment_label(sent_current)
        dist[label] += 1
        if sent_current is not None:
            sentiment_scores.append(sent_current)

        sector = co.get("sector", "") or "Unknown"
        sector_map.setdefault(sector, [])
        if sent_current is not None:
            sector_map[sector].append(sent_current)

        chg1d = (co.get("market") or {}).get("change_pct_1d")
        if chg1d is not None:
            changes_1d.append(chg1d)

    sector_breakdown = {
        sector: {
            "count": len(scores),
            "avg_sentiment": round(mean(scores), 3) if scores else None,
        }
        for sector, scores in sorted(sector_map.items())
    }

    ok = [c for c in company_results if not c.get("error")]

    def _sort_key_sent(c: dict) -> float:
        v = (c.get("signals") or {}).get("sentiment", {}).get("momentum")
        return v if v is not None else 0.0

    def _sort_key_price(c: dict) -> float:
        v = (c.get("market") or {}).get("change_pct_1d")
        return v if v is not None else 0.0

    top_movers_sentiment = [
        {"name": c["name"], "id": c["id"],
         "sentiment_momentum": (c.get("signals") or {}).get("sentiment", {}).get("momentum")}
        for c in sorted(ok, key=_sort_key_sent, reverse=True)[:5]
    ]
    bottom_movers_sentiment = [
        {"name": c["name"], "id": c["id"],
         "sentiment_momentum": (c.get("signals") or {}).get("sentiment", {}).get("momentum")}
        for c in sorted(ok, key=_sort_key_sent)[:5]
    ]
    top_movers_price = [
        {"name": c["name"], "id": c["id"],
         "change_pct_1d": (c.get("market") or {}).get("change_pct_1d")}
        for c in sorted(ok, key=_sort_key_price, reverse=True)[:5]
        if (c.get("market") or {}).get("change_pct_1d") is not None
    ]
    bottom_movers_price = [
        {"name": c["name"], "id": c["id"],
         "change_pct_1d": (c.get("market") or {}).get("change_pct_1d")}
        for c in sorted(ok, key=_sort_key_price)[:5]
        if (c.get("market") or {}).get("change_pct_1d") is not None
    ]

    return {
        "sentiment_distribution": dist,
        "sector_breakdown": sector_breakdown,
        "top_movers_sentiment": top_movers_sentiment,
        "bottom_movers_sentiment": bottom_movers_sentiment,
        "top_movers_price_1d": top_movers_price,
        "bottom_movers_price_1d": bottom_movers_price,
        "median_sentiment_score": round(median(sentiment_scores), 3) if sentiment_scores else None,
        "median_change_pct_1d": round(median(changes_1d), 2) if changes_1d else None,
    }


def run_snapshot(
    entities: list[str],
    include_market: bool = True,
    max_workers: int = 30,
) -> dict:
    """Fast portfolio snapshot: signals + market data, no chunks, no LLM.

    Target latency: ≤5s for 30 companies with default workers.
    """
    t0 = time.monotonic()
    companies = resolve_entities(entities, max_workers=max_workers)

    results: list[dict] = [None] * len(companies)  # type: ignore[list-item]

    def _run_one(idx: int, company: dict) -> tuple[int, dict]:
        if company.get("error") or not company.get("id"):
            return idx, company
        try:
            data = _fetch_company_data(company, days=90, include_market=include_market, include_chunks=False)
            data.pop("_raw_chunks", None)
            data.pop("_chunk_days", None)
            return idx, _slim_company(data)
        except Exception as exc:
            return idx, {**company, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=min(max_workers, len(companies) or 1)) as ex:
        futures = {ex.submit(_run_one, i, co): i for i, co in enumerate(companies)}
        for fut in as_completed(futures):
            idx, data = fut.result()
            results[idx] = data

    wall_time = round(time.monotonic() - t0, 2)
    summary = _build_portfolio_summary(results)

    resolved = sum(1 for r in results if not r.get("error") and r.get("id"))
    failed = len(results) - resolved

    metadata: dict = {
        "total_companies": len(entities),
        "resolved": resolved,
        "failed": failed,
        "wall_time_s": wall_time,
    }
    if len(entities) > max_workers * 2:
        metadata["capacity_warning"] = (
            f"Input has {len(entities)} companies but max_workers={max_workers}. "
            f"Consider increasing max_workers or reducing portfolio size for ≤10s latency."
        )

    return {
        "metadata": metadata,
        "portfolio_summary": summary,
        "companies": results,
    }


def run_portfolio(
    entities: list[str],
    days: int = 30,
    include_narrative: bool = False,
    include_market: bool = True,
    max_workers: int = 30,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Full portfolio analysis: signals + chunks + optional LLM narrative + market data.

    Target latency: ≤10s for 30 companies without narrative, ≤10s for ~20 with narrative.
    """
    t0 = time.monotonic()
    openai_client = OpenAI(api_key=OPENAI_API_KEY) if include_narrative else None
    companies = resolve_entities(entities, max_workers=max_workers)

    results: list[dict] = [None] * len(companies)  # type: ignore[list-item]

    def _run_one(idx: int, company: dict) -> tuple[int, dict]:
        if company.get("error") or not company.get("id"):
            return idx, company
        try:
            data = _fetch_company_data(
                company, days=days,
                include_market=include_market,
                include_chunks=True,
            )
            raw_chunks = data.pop("_raw_chunks", [])
            actual_days = data.pop("_chunk_days", days)

            docs = aggregate_to_docs(raw_chunks, TOP_DOCS)

            if include_narrative and openai_client is not None and (raw_chunks or docs):
                data["narrative"] = generate_narrative(
                    openai_client, company, data["signals"], docs, actual_days, model, len(raw_chunks)
                )
            elif include_narrative:
                data["narrative"] = None

            # Slim evidence: counts + doc table only (no raw text bulk)
            data["evidence"] = {
                "raw_chunks": len(raw_chunks),
                "documents": len(docs),
                "window_days": actual_days,
                "doc_table": [
                    {
                        "rank":      d.get("rank"),
                        "date":      (d.get("timestamp") or "")[:10],
                        "source":    d.get("source"),
                        "headline":  (d.get("headline") or "")[:120],
                        "relevance": d.get("relevance"),
                        "sentiment": d.get("sentiment"),
                        "snippets":  d.get("snippets"),
                    }
                    for d in docs
                ],
            }

            return idx, _slim_company(data)
        except Exception as exc:
            return idx, {**company, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=min(max_workers, len(companies) or 1)) as ex:
        futures = {ex.submit(_run_one, i, co): i for i, co in enumerate(companies)}
        for fut in as_completed(futures):
            idx, data = fut.result()
            results[idx] = data

    wall_time = round(time.monotonic() - t0, 2)
    summary = _build_portfolio_summary(results)

    resolved = sum(1 for r in results if not r.get("error") and r.get("id"))
    failed = len(results) - resolved

    metadata: dict = {
        "total_companies": len(entities),
        "resolved": resolved,
        "failed": failed,
        "wall_time_s": wall_time,
        "days": days,
        "include_narrative": include_narrative,
        "include_market": include_market,
    }
    if len(entities) > max_workers * 2:
        metadata["capacity_warning"] = (
            f"Input has {len(entities)} companies but max_workers={max_workers}. "
            f"Consider increasing max_workers or reducing portfolio size for ≤10s latency."
        )

    return {
        "metadata": metadata,
        "portfolio_summary": summary,
        "companies": results,
    }

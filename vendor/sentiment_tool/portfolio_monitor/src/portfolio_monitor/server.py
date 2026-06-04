"""Portfolio Monitor MCP server — stdio transport."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from portfolio_monitor.portfolio import run_portfolio, run_snapshot

mcp = FastMCP("portfolio-monitor")


@mcp.tool(name="analyze_portfolio")
def analyze_portfolio(
    entities: list[str],
    days: int = 30,
    include_narrative: bool = True,
    include_market: bool = True,
    max_workers: int = 30,
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    """Run portfolio-level sentiment + market analysis across a list of companies.

    Returns per-company signals, evidence doc tables, LLM narratives, and market
    data (price, changes across 1D/5D/1M/3M/6M/YTD/1Y), plus a portfolio-level
    summary with sentiment distribution, sector breakdown, and top/bottom movers.

    For deep dives on a single company, use the bigdata MCP tools:
      - bigdata_sentiment_tearsheet — full 90-day sentiment breakdown
      - bigdata_company_tearsheet   — comprehensive company profile

    Args:
        entities: List of rp_entity_ids (e.g. "D8442A") or company names/tickers
                  (e.g. "Apple", "AAPL"). Both formats can be mixed.
        days: Chunk lookback in days: 7 | 30 | 90. Signals always use 90d. Default 30.
        include_narrative: Generate an LLM analyst narrative per company. Default True.
                           Set False for faster runs on large portfolios (>20 companies).
        include_market: Fetch real-time quote and price changes. Default True.
        max_workers: Outer concurrency (default 30, max 40).
        model: OpenAI model for narrative generation. Default gpt-4o-mini.

    Returns:
        metadata, portfolio_summary, companies.
    """
    days = max(1, min(days, 90))
    max_workers = max(1, min(max_workers, 40))
    return run_portfolio(
        entities=entities,
        days=days,
        include_narrative=include_narrative,
        include_market=include_market,
        max_workers=max_workers,
        model=model,
    )


@mcp.tool(name="get_market_snapshot")
def get_market_snapshot(
    entities: list[str],
    include_market: bool = True,
    max_workers: int = 30,
) -> dict[str, Any]:
    """Fast portfolio snapshot: sentiment signals + media attention + market data.

    No chunk fetch, no LLM narrative. Returns sentiment score, momentum, z-scores,
    and real-time market data per company. Target latency ≤5s for 50 companies.

    For deeper analysis with evidence docs and narrative use analyze_portfolio.

    Args:
        entities: List of rp_entity_ids or company names/tickers (mixed OK).
        include_market: Fetch real-time quote and price changes. Default True.
        max_workers: Outer concurrency. Default 30.

    Returns:
        metadata, portfolio_summary, companies.
    """
    max_workers = max(1, min(max_workers, 40))
    return run_snapshot(
        entities=entities,
        include_market=include_market,
        max_workers=max_workers,
    )


def main() -> None:
    """Run the portfolio monitor MCP server over stdio."""
    mcp.run()

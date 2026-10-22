"""
Pricing configuration for LLM and embedding models.
Prices are in USD per 1 million tokens.

Standard (non-cached) chat rates; reconcile periodically with official tables:
https://platform.openai.com/docs/pricing

Last reviewed against that page: April 2026.

NOTE on reasoning models (o1 / o3 / o4):
  OpenAI charges reasoning tokens as output tokens. The LLMUsage.completion_tokens
  field already includes them (the API reports total output + reasoning in usage),
  so no special handling is needed here — the output price covers both.
"""
import time
from dataclasses import dataclass
from threading import Lock

import httpx

from bigdata_briefs import logger
from bigdata_briefs.settings import settings


@dataclass
class ModelPricing:
    """Pricing for a single model."""

    input_price_per_1m: float  # USD per 1M input tokens
    output_price_per_1m: float  # USD per 1M output tokens (0 for embeddings)

    def calculate_cost(
        self,
        input_tokens: int,
        output_tokens: int = 0,
    ) -> float:
        """Calculate total cost in USD."""
        input_cost = (input_tokens / 1_000_000) * self.input_price_per_1m
        output_cost = (output_tokens / 1_000_000) * self.output_price_per_1m
        return input_cost + output_cost


# OpenAI Standard API pricing (per 1M tokens). Batch / Flex / Priority differ; see docs.
MODEL_PRICING: dict[str, ModelPricing] = {
    # ── GPT-4o family ─────────────────────────────────────────────────────────
    "gpt-4o": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-2024-11-20": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-2024-08-06": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-mini": ModelPricing(input_price_per_1m=0.15, output_price_per_1m=0.60),
    "gpt-4o-mini-2024-07-18": ModelPricing(input_price_per_1m=0.15, output_price_per_1m=0.60),
    # ── GPT-4.1 family ────────────────────────────────────────────────────────
    "gpt-4.1": ModelPricing(input_price_per_1m=2.00, output_price_per_1m=8.00),
    "gpt-4.1-mini": ModelPricing(input_price_per_1m=0.40, output_price_per_1m=1.60),
    "gpt-4.1-nano": ModelPricing(input_price_per_1m=0.10, output_price_per_1m=0.40),
    # ── GPT-5 family ──────────────────────────────────────────────────────────
    "gpt-5": ModelPricing(input_price_per_1m=1.25, output_price_per_1m=10.00),
    "gpt-5.1": ModelPricing(input_price_per_1m=1.25, output_price_per_1m=10.00),
    "gpt-5.2": ModelPricing(input_price_per_1m=1.75, output_price_per_1m=14.00),
    "gpt-5-mini": ModelPricing(input_price_per_1m=0.25, output_price_per_1m=2.00),
    "gpt-5-mini-2025-08-07": ModelPricing(input_price_per_1m=0.25, output_price_per_1m=2.00),
    "gpt-5-nano": ModelPricing(input_price_per_1m=0.05, output_price_per_1m=0.40),
    "gpt-5-pro": ModelPricing(input_price_per_1m=15.00, output_price_per_1m=120.00),
    "gpt-5.2-pro": ModelPricing(input_price_per_1m=21.00, output_price_per_1m=168.00),
    # ── o1 family (reasoning) ─────────────────────────────────────────────────
    "o1": ModelPricing(input_price_per_1m=15.00, output_price_per_1m=60.00),
    "o1-preview": ModelPricing(input_price_per_1m=15.00, output_price_per_1m=60.00),
    "o1-mini": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    "o1-mini-2024-09-12": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    # ── o3 family (reasoning) ─────────────────────────────────────────────────
    "o3": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=40.00),
    "o3-mini": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    "o3-mini-2025-01-31": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    # ── o4 family (reasoning) ─────────────────────────────────────────────────
    "o4-mini": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    "o4-mini-2025-04-16": ModelPricing(input_price_per_1m=1.10, output_price_per_1m=4.40),
    # ── GPT-4 Turbo (legacy) ──────────────────────────────────────────────────
    "gpt-4-turbo": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    "gpt-4-turbo-preview": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    "gpt-4-turbo-2024-04-09": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    # ── GPT-4 (legacy) ────────────────────────────────────────────────────────
    "gpt-4": ModelPricing(input_price_per_1m=30.00, output_price_per_1m=60.00),
    "gpt-4-0613": ModelPricing(input_price_per_1m=30.00, output_price_per_1m=60.00),
    # ── GPT-3.5 (legacy) ──────────────────────────────────────────────────────
    "gpt-3.5-turbo": ModelPricing(input_price_per_1m=0.50, output_price_per_1m=1.50),
    "gpt-3.5-turbo-0125": ModelPricing(input_price_per_1m=0.50, output_price_per_1m=1.50),
    # ── Embedding models ──────────────────────────────────────────────────────
    "text-embedding-3-large": ModelPricing(input_price_per_1m=0.13, output_price_per_1m=0.0),
    "text-embedding-3-small": ModelPricing(input_price_per_1m=0.02, output_price_per_1m=0.0),
    "text-embedding-ada-002": ModelPricing(input_price_per_1m=0.10, output_price_per_1m=0.0),
}

# Default pricing for unknown models (conservative estimate)
DEFAULT_PRICING = ModelPricing(input_price_per_1m=1.00, output_price_per_1m=3.00)


def get_model_pricing(model: str) -> ModelPricing:
    """Get pricing for a model, with fallback to default."""
    return MODEL_PRICING.get(model, DEFAULT_PRICING)


def calculate_llm_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Calculate cost for an LLM call in USD."""
    pricing = get_model_pricing(model)
    return pricing.calculate_cost(prompt_tokens, completion_tokens)


def calculate_embedding_cost(model: str, tokens: int) -> float:
    """Calculate cost for embedding tokens in USD."""
    pricing = get_model_pricing(model)
    return pricing.calculate_cost(tokens, 0)


# Bigdata search is billed in retrieval tokens, reported per request in the
# response's ``usage`` block, one counter per content tier (``web_tokens``,
# ``premium_news_tokens``, ...). Each tier has its own price, which we read
# from GET /v1/subscription/quotas for our key, so the cost is what the API
# actually debits from the account's credits.
_TOKEN_PRICES_TTL_S = 6 * 3600

# Used when /v1/subscription/quotas is unreachable: USD per token on our key
# as of 2026-10-22.
FALLBACK_TOKEN_PRICES_USD: dict[str, float] = {
    "premium_news_tokens": 96e-6,
    "expert_interviews_tokens": 98e-6,
    "podcasts_tokens": 74e-6,
    "earnings_transcripts_tokens": 66e-6,
    "corporate_communications_tokens": 36e-6,
    "regulatory_filings_tokens": 10e-6,
    "web_tokens": 8e-6,
    "private_data_tokens": 8e-6,
}

_token_prices: dict[str, float] | None = None
_token_prices_fetched_at: float = 0.0
_token_prices_lock = Lock()


def _usage_key_for_unit(unit_id: str) -> str | None:
    """Map a fast-search billing unit id to its ``usage`` counter name.

    ``search:fast:content-premium-news:tokens`` -> ``premium_news_tokens``.
    """
    parts = unit_id.split(":")
    if len(parts) != 4 or parts[:2] != ["search", "fast"] or not parts[2].startswith("content-"):
        return None
    return parts[2].removeprefix("content-").replace("-", "_") + "_tokens"


def _fetch_token_prices() -> dict[str, float]:
    resp = httpx.get(
        f"{settings.API_BASE_URL}/v1/subscription/quotas",
        headers={"X-API-KEY": str(settings.BIGDATA_API_KEY)},
        timeout=20,
    )
    resp.raise_for_status()
    prices: dict[str, float] = {}
    for billing in resp.json()["results"].get("billing") or []:
        for unit in billing.get("units") or []:
            key = _usage_key_for_unit(unit.get("id", ""))
            if key and unit.get("units_price") is not None:
                # units_price is US cents per single token
                prices[key] = float(unit["units_price"]) / 100
    if not prices:
        raise ValueError("no search token prices in /v1/subscription/quotas")
    return prices


def get_token_prices() -> dict[str, float]:
    """USD price per token for each ``usage`` counter, cached for a few hours."""
    global _token_prices, _token_prices_fetched_at
    with _token_prices_lock:
        if _token_prices is not None and time.monotonic() - _token_prices_fetched_at < _TOKEN_PRICES_TTL_S:
            return _token_prices
        try:
            _token_prices = _fetch_token_prices()
        except Exception as e:
            logger.warning(f"Could not read token prices from /v1/subscription/quotas, using fallback: {e}")
            _token_prices = _token_prices or dict(FALLBACK_TOKEN_PRICES_USD)
        _token_prices_fetched_at = time.monotonic()
        return _token_prices


def retrieval_tokens_from_usage(usage: dict | None) -> dict[str, int]:
    """Per-tier token counters (``*_tokens``) from a Bigdata search ``usage`` block."""
    if not usage:
        return {}
    return {
        k: int(v) for k, v in usage.items()
        if k.endswith("_tokens") and isinstance(v, (int, float))
    }


def merge_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Sum two per-tier token dicts."""
    out = dict(a)
    for tier, n in b.items():
        out[tier] = out.get(tier, 0) + n
    return out


def calculate_retrieval_cost(tokens_by_tier: dict[str, int]) -> float:
    """Cost in USD of per-tier retrieval tokens at our key's prices.

    A tier with no price on our key costs 0 here (and is logged); its tokens
    are still recorded so they stay visible.
    """
    prices = get_token_prices()
    cost = 0.0
    for tier, tokens in tokens_by_tier.items():
        price = prices.get(tier)
        if price is None:
            logger.warning(f"No token price for usage counter {tier!r}; counted at $0")
            continue
        cost += tokens * price
    return cost


# Legacy per-chunk price, used only for runs recorded before retrieval tokens
# were tracked (their SQLRunMetrics.retrieval_cost_usd is NULL).
CHUNK_UNIT_PRICE_USD: float = 0.0015


def calculate_chunk_cost(chunks: int) -> float:
    """Calculate legacy cost for retrieved search chunks in USD."""
    return chunks * CHUNK_UNIT_PRICE_USD


def calculate_search_cost(chunks: int, retrieval_cost_usd: float | None) -> float:
    """Bigdata search cost for a run (or step): the token cost recorded at run
    time when there is one, legacy per-chunk cost otherwise."""
    if retrieval_cost_usd is not None:
        return retrieval_cost_usd
    return calculate_chunk_cost(chunks or 0)


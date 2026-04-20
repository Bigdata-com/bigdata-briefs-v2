"""
Pricing configuration for LLM and embedding models.
Prices are in USD per 1 million tokens.

Standard (non-cached) chat rates; reconcile periodically with official tables:
https://platform.openai.com/docs/pricing

Last reviewed against that page: March 2026.
"""
from dataclasses import dataclass


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
    # GPT-4o family
    "gpt-4o": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-mini": ModelPricing(input_price_per_1m=0.15, output_price_per_1m=0.60),
    "gpt-4o-2024-11-20": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-2024-08-06": ModelPricing(input_price_per_1m=2.50, output_price_per_1m=10.00),
    "gpt-4o-mini-2024-07-18": ModelPricing(input_price_per_1m=0.15, output_price_per_1m=0.60),
    # GPT-4.1 family (Standard tier on platform.openai.com/docs/pricing)
    "gpt-4.1": ModelPricing(input_price_per_1m=2.00, output_price_per_1m=8.00),
    "gpt-4.1-mini": ModelPricing(input_price_per_1m=0.40, output_price_per_1m=1.60),
    # GPT-5 family (Standard tier)
    "gpt-5-mini": ModelPricing(input_price_per_1m=0.25, output_price_per_1m=2.00),
    # GPT-4 Turbo
    "gpt-4-turbo": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    "gpt-4-turbo-preview": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    "gpt-4-turbo-2024-04-09": ModelPricing(input_price_per_1m=10.00, output_price_per_1m=30.00),
    # GPT-4
    "gpt-4": ModelPricing(input_price_per_1m=30.00, output_price_per_1m=60.00),
    "gpt-4-0613": ModelPricing(input_price_per_1m=30.00, output_price_per_1m=60.00),
    # GPT-3.5
    "gpt-3.5-turbo": ModelPricing(input_price_per_1m=0.50, output_price_per_1m=1.50),
    "gpt-3.5-turbo-0125": ModelPricing(input_price_per_1m=0.50, output_price_per_1m=1.50),
    # Embedding models
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


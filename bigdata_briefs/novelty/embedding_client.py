import traceback
from typing import TYPE_CHECKING

import openai

from bigdata_briefs import logger
from bigdata_briefs.metrics import EmbeddingsMetrics, StepMetrics
from bigdata_briefs.models import EmbeddingsUsage
from bigdata_briefs.pricing import calculate_embedding_cost
from bigdata_briefs.settings import settings
from bigdata_briefs.utils import sleep_with_backoff

if TYPE_CHECKING:
    from bigdata_briefs.metrics import EntityStepMetrics


class EmbeddingClient:
    def __init__(self, model: str, client: openai.OpenAI | None = None):
        self.model = model
        if client is None:
            client = openai.OpenAI(api_key=str(settings.OPENAI_API_KEY))
        self.client = client

    def compute(
        self, 
        texts: list[str], 
        step_name: str | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
        **kwargs,
    ) -> list[list[float]]:
        try:
            response = self._embeddings_with_retries(
                func=self.client.embeddings.create,
                input=texts,
                model=self.model,
                encoding_format="float",
            )

            embeddings = [embedding.embedding for embedding in response.data]
            token_count = response.usage.prompt_tokens

        except Exception:
            logger.error(
                f"Error computing embeddings {self.model=}, {texts=}, {kwargs=} {traceback.format_exc()}"
            )
            raise

        cost = calculate_embedding_cost(self.model, token_count)

        embedding_usage = EmbeddingsUsage(
            model=self.model, tokens=token_count, cost_usd=cost
        )
        EmbeddingsMetrics.track_usage(embedding_usage)
        
        # Track per-step usage with entity_metrics (preferred) or fallback to global StepMetrics
        if entity_metrics:
            entity_metrics.track_embedding_usage(embedding_usage)
        else:
            effective_step = step_name or StepMetrics.get_current_step()
            if effective_step:
                StepMetrics.track_embedding_usage(effective_step, embedding_usage)

        return embeddings

    def _embeddings_with_retries(self, func, *args, **kwargs):
        for attempt in range(settings.EMBEDDING_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception:
                if attempt >= settings.EMBEDDING_RETRIES - 1:
                    raise
                sleep_with_backoff(attempt=attempt)

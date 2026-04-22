import json
import random
import time
from typing import TYPE_CHECKING
from uuid import UUID

import openai

from bigdata_briefs import logger
from bigdata_briefs.debug_logger import DebugLogger
from bigdata_briefs.metrics import LLMMetrics, StepMetrics
from bigdata_briefs.models import LLMUsage

if TYPE_CHECKING:
    from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.pricing import calculate_llm_cost
from bigdata_briefs.settings import settings
from bigdata_briefs.utils import (
    log_args,
    log_return_value,
    log_time,
    sleep_with_backoff,
)


class LLMClient:
    def __init__(self, client: openai.OpenAI | None = None, debug_logger: DebugLogger | None = None):
        if client is None:
            client = openai.OpenAI(
                api_key=str(settings.OPENAI_API_KEY),
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
        self.client = client
        self.debug_logger = debug_logger

    @log_time
    @log_args
    @log_return_value
    def call_with_response_format(
        self,
        *args,
        system: list,
        messages: list,
        model: str,
        max_tokens: int,
        step_name: str | None = None,
        debug_metadata: dict | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
        **kwargs,
    ):
        messages = system + messages
        logger.debug(
            f"Calling {model} with messages: \n {json.dumps(messages, indent=2)}"
        )
        # Reasoning models use reasoning={"effort": "low"} instead of temperature
        api_kwargs = dict(kwargs)
        reasoning_effort = api_kwargs.pop("reasoning_effort", None)
        if reasoning_effort is not None:
            api_kwargs["reasoning"] = {"effort": reasoning_effort}
        if entity_metrics is not None and step_name:
            entity_metrics.start_step(step_name)
        try:
            response = self._call_with_retries(
                self.client.responses.parse,
                *args,
                input=messages,
                model=model,
                max_output_tokens=max_tokens,
                _log_context={"step_name": step_name, "model": model},
                **api_kwargs,
            )

            cost = calculate_llm_cost(
                model=model,
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
            )

            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
                "cost_usd": cost,
            }

            llm_usage = LLMUsage(
                model=model,
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
                cost_usd=cost,
            )
            LLMMetrics.track_usage(llm_usage)

            # Prefer explicit step_name so nested LLM calls under e.g. novelty_check stay attributed
            # to novelty_embedding_* / relevance_* rows in step_metrics.json.
            if entity_metrics:
                entity_metrics.track_llm_usage(
                    llm_usage,
                    attributee_step=step_name if step_name else None,
                )
            else:
                effective_step = step_name or StepMetrics.get_current_step()
                if effective_step:
                    StepMetrics.track_llm_usage(effective_step, llm_usage)

            content = response.output_parsed

            effective_logger = debug_logger or self.debug_logger
            if effective_logger and step_name:
                try:
                    if hasattr(content, "model_dump"):
                        response_dict = content.model_dump()
                    elif hasattr(content, "dict"):
                        response_dict = content.dict()
                    else:
                        response_dict = {"response": str(content)}

                    effective_logger.save_llm_call(
                        step_name=step_name,
                        model=model,
                        system_prompt=system,
                        user_messages=messages,
                        response=response_dict,
                        usage=usage,
                        debug_metadata=debug_metadata,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save LLM debug log: {e}")

            step_display = step_name or "llm_call"
            logger.debug(
                "LLM call done step=%s model=%s total_tokens=%s",
                step_display,
                model,
                usage["total_tokens"],
            )

            return content
        finally:
            if entity_metrics is not None and step_name:
                entity_metrics.end_step(step_name)

    @log_time
    @log_args
    @log_return_value
    def call_without_response_format(
        self,
        *args,
        messages: list,
        model: str,
        max_tokens: int,
        step_name: str | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
        **kwargs,
    ):
        logger.debug(
            f"Calling {model} with messages: \n {json.dumps(messages, indent=2)}"
        )
        # Reasoning models use reasoning={"effort": "low"} instead of temperature
        api_kwargs = dict(kwargs)
        reasoning_effort = api_kwargs.pop("reasoning_effort", None)
        if reasoning_effort is not None:
            api_kwargs["reasoning"] = {"effort": reasoning_effort}
        if entity_metrics is not None and step_name:
            entity_metrics.start_step(step_name)
        try:
            response = self._call_with_retries(
                self.client.chat.completions.create,
                *args,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                **api_kwargs,
            )

            cost = calculate_llm_cost(
                model=model,
                prompt_tokens=response["usage"]["inputTokens"],
                completion_tokens=response["usage"]["outputTokens"],
            )

            usage = {
                "prompt_tokens": response["usage"]["inputTokens"],
                "completion_tokens": response["usage"]["outputTokens"],
                "total_tokens": response["usage"]["totalTokens"],
                "cost_usd": cost,
            }

            llm_usage = LLMUsage(
                model=model,
                prompt_tokens=response["usage"]["inputTokens"],
                completion_tokens=response["usage"]["outputTokens"],
                total_tokens=response["usage"]["totalTokens"],
                cost_usd=cost,
            )
            LLMMetrics.track_usage(llm_usage)

            if entity_metrics:
                entity_metrics.track_llm_usage(
                    llm_usage,
                    attributee_step=step_name if step_name else None,
                )
            else:
                effective_step = step_name or StepMetrics.get_current_step()
                if effective_step:
                    StepMetrics.track_llm_usage(effective_step, llm_usage)

            text_response = response["output"]["message"]["content"][0]["text"]

            effective_logger = debug_logger or self.debug_logger
            if effective_logger and step_name:
                try:
                    effective_logger.save_llm_call(
                        step_name=step_name,
                        model=model,
                        system_prompt=None,
                        user_messages=messages,
                        response=text_response,
                        usage=usage,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save LLM debug log: {e}")

            step_display = step_name or "llm_call"
            logger.debug(
                "LLM call done step=%s model=%s total_tokens=%s",
                step_display,
                model,
                usage["total_tokens"],
            )

            return text_response
        finally:
            if entity_metrics is not None and step_name:
                entity_metrics.end_step(step_name)

    # Seconds to wait after a 429 RateLimitError, indexed by attempt number (0-based).
    # attempt 0 → 30s, attempt 1 → 60s, any further attempt → 60s.
    _RATE_LIMIT_WAITS: list[int] = [30, 60]

    def _call_with_retries(self, func, *args, **kwargs):
        log_ctx = kwargs.pop("_log_context", None) or {}
        step_name = log_ctx.get("step_name")
        model = log_ctx.get("model")
        for attempt in range(settings.LLM_RETRIES):
            try:
                return func(*args, **kwargs)
            except openai.RateLimitError as e:
                if attempt >= settings.LLM_RETRIES - 1:
                    raise
                ctx_str = " ".join(f"{k}={v}" for k, v in log_ctx.items() if v)
                # Respect the Retry-After header when present; otherwise use fixed schedule.
                retry_after: float | None = None
                try:
                    retry_after = float(e.response.headers.get("retry-after", ""))
                except (AttributeError, TypeError, ValueError):
                    pass
                if retry_after is None:
                    retry_after = float(
                        self._RATE_LIMIT_WAITS[min(attempt, len(self._RATE_LIMIT_WAITS) - 1)]
                    )
                jitter = random.uniform(0, 2)
                wait = retry_after + jitter
                logger.warning(
                    "OpenAI RateLimitError (429) — waiting %.1fs before retry. "
                    "Attempt %s/%s%s",
                    wait,
                    attempt + 1,
                    settings.LLM_RETRIES,
                    f" ({ctx_str})" if ctx_str else "",
                )
                time.sleep(wait)
            except openai.APITimeoutError as e:
                if attempt >= settings.LLM_RETRIES - 1:
                    raise
                ctx_str = " ".join(f"{k}={v}" for k, v in log_ctx.items() if v)
                logger.warning(
                    "OpenAI timeout (%.0fs) — retrying. Attempt %s/%s%s",
                    settings.LLM_TIMEOUT_SECONDS,
                    attempt + 1,
                    settings.LLM_RETRIES,
                    f" ({ctx_str})" if ctx_str else "",
                )
                sleep_with_backoff(attempt=attempt)
            except Exception as e:
                if attempt >= settings.LLM_RETRIES - 1:
                    raise
                ctx_str = " ".join(f"{k}={v}" for k, v in log_ctx.items() if v)
                logger.warning(
                    "Error calling LLM: %s. Attempt %s/%s%s",
                    e,
                    attempt + 1,
                    settings.LLM_RETRIES,
                    f" ({ctx_str})" if ctx_str else "",
                )
                sleep_with_backoff(attempt=attempt)

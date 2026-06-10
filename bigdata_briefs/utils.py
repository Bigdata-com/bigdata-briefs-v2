import asyncio
import random
import time
import traceback
import warnings
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from time import perf_counter
from typing import TYPE_CHECKING, Type

if TYPE_CHECKING:
    from collections.abc import Generator

    from bigdata_briefs.metrics import EntityStepMetrics

from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from bigdata_briefs import logger


def log_time(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            logger.debug(f"{func.__name__} executed in {perf_counter() - start}s")

    return wrapper


def log_args(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger.debug(f"{func.__name__} executed with {args=} and {kwargs=}")
        return func(*args, **kwargs)

    return wrapper


def log_return_value(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        value = func(*args, **kwargs)
        logger.debug(f"{func.__name__} returned {value=}")
        return value

    return wrapper


def log_performance(func):
    @wraps(func)
    def wrapper(
        *args, enable_metric: bool = False, metric_name: str = "Undefined", **kwargs
    ):
        start = perf_counter()
        value = func(*args, **kwargs)
        if enable_metric:
            timing = datetime.now()
            msg = f"{timing.strftime('%I:%M:%S')}.{timing.microsecond // 10000:02d} - {metric_name} - {round(time.perf_counter() - start, 2)}"
            logger.debug(msg)
        return value

    return wrapper


def validate_and_repair_model(json_str: str, model: Type[BaseModel]) -> BaseModel:
    try:
        response = model.model_validate_json(json_str)
        return response
    except ValidationError:
        # With return_objects=False, it always returns a string, so ignore type checking error
        fixed_json_str: str = repair_json(json_str, return_objects=False)  # type: ignore[invalid-assignment]
        try:
            response = model.model_validate_json(fixed_json_str)
            logger.debug(
                f"The following could not be parsed as a {model.__name__}, but we could repair the json\n{json_str=}\n{fixed_json_str=}"
            )
            return response
        except ValidationError:
            logger.warning(
                f"The following LLM completion could not be parsed as a {model.__name__}, nor could it be repaired\n{json_str=}\n{fixed_json_str=}"
            )
            raise


def sleep_with_backoff(*, base: int = 1, attempt: int):
    """
    Sleeps for an amount of time. This amount is calculated
    with backoff and jitter taking into account the number
    of retries.

    @attempt starts at 0

    """
    max_sleep = 20

    rnd_upper_bound = min(max_sleep, base * 2**attempt)
    sleep_time = round(random.uniform(0.5, rnd_upper_bound), 2)

    logger.debug(f"Sleeping for {sleep_time}")

    time.sleep(sleep_time)


async def asleep_with_backoff(*, base: int = 1, attempt: int):
    """Async mirror of :func:`sleep_with_backoff`.

    Same backoff + jitter schedule, but awaits ``asyncio.sleep`` instead of
    blocking the thread, for use inside async code paths (e.g. the novelty-search
    HTTP fetch).

    @attempt starts at 0
    """
    max_sleep = 20

    rnd_upper_bound = min(max_sleep, base * 2**attempt)
    sleep_time = round(random.uniform(0.5, rnd_upper_bound), 2)

    logger.debug(f"Sleeping for {sleep_time}")

    await asyncio.sleep(sleep_time)


def raise_warning_from(e, category=RuntimeWarning):
    """Issue a warning derived from an exception object."""
    tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    warnings.warn(
        f"Converted exception to warning:\n{tb_str.strip()}", category, stacklevel=2
    )


@contextmanager
def track_step(
    step_name: str, 
    metrics: "EntityStepMetrics | None" = None,
) -> "Generator[None, None, None]":
    """Context manager to track cost and time for a pipeline step.
    
    Usage:
        # With per-entity metrics (recommended for parallel processing):
        with track_step("concept_extraction", entity_metrics):
            # ... step code ...
        
        # Without metrics (legacy, uses global StepMetrics):
        with track_step("entity_grounding"):
            # ... step code ...
    
    Args:
        step_name: Name of the step to track
        metrics: Optional EntityStepMetrics instance for per-entity tracking
    
    This tracks:
    - Execution time (start to end of context)
    - LLM costs (via track_llm_usage)
    - Embedding costs (via track_embedding_usage)
    
    All LLM/embedding calls within this context will automatically be attributed
    to this step when using entity_metrics.
    """
    from bigdata_briefs.metrics import EntityStepMetrics, StepMetrics
    
    if metrics:
        # Use per-entity metrics (thread-safe for parallel processing)
        from datetime import datetime, timezone

        wall_started = datetime.now(timezone.utc)
        metrics.start_step(step_name)
        metrics.set_current_step(step_name)
        try:
            yield
        finally:
            metrics.set_current_step(None)
            metrics.end_step(step_name)
            wall_ended = datetime.now(timezone.utc)
            metrics.record_pipeline_step_wall(step_name, wall_started, wall_ended)
    else:
        # Fallback to global StepMetrics (legacy behavior)
        StepMetrics.start_step(step_name)
        StepMetrics.set_current_step(step_name)
        try:
            yield
        finally:
            StepMetrics.set_current_step(None)
            StepMetrics.end_step(step_name)

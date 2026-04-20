from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue
from threading import Lock
from time import perf_counter

from bigdata_briefs import logger
from bigdata_briefs.models import (
    BulletPointsUsage,
    EmbeddingsUsage,
    LLMUsage,
    StepUsage,
    TopicContentTracker,
)


class Metrics(ABC):
    @classmethod
    @abstractmethod
    def track_usage(cls, usage): ...

    @classmethod
    @abstractmethod
    def get_total_usage(cls): ...

    @classmethod
    def reset_usage(cls):
        with cls.lock:
            cls.metrics_queue.queue.clear()


class CacheMetrics(Metrics):
    metrics_queue = Queue()
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: int = 1):
        cls.metrics_queue.put(usage)

    @classmethod
    def get_total_usage(cls) -> int:
        with cls.lock:
            usages = cls.metrics_queue.queue
            if not usages:
                return 0
            return sum(usages)


class QueryUnitMetrics(Metrics):
    metrics_queue = Queue()
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: int):
        cls.metrics_queue.put(usage)

    @classmethod
    def get_total_usage(cls) -> int:
        with cls.lock:
            usages = cls.metrics_queue.queue
            if not usages:
                return 0
            return sum(usages)


class WarningsMetrics(Metrics):
    metrics_queue = Queue()
    warnings = set()
    lock = Lock()

    @classmethod
    def track_usage(cls, warning_message: str):
        with cls.lock:
            # Avoid logging duplicate warnings
            if warning_message not in cls.warnings:
                logger.info("A warning have been suppressed", warning=warning_message)
            cls.warnings.add(warning_message)

    @classmethod
    def get_total_usage(cls) -> set[str]:
        with cls.lock:
            return cls.warnings.copy()


class BulletPointMetrics(Metrics):
    metrics_queue = Queue()
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: BulletPointsUsage):
        cls.metrics_queue.put(usage)

    @classmethod
    def get_total_usage(cls):
        with cls.lock:
            usages = cls.metrics_queue.queue
            if not usages:
                return BulletPointsUsage()

            return sum(cls.metrics_queue.queue, start=BulletPointsUsage())


class LLMMetrics:
    """
    This class does not inherit from Metrics, this is a more complex class and it doesn't work with
    a queue. It tracks usage per model and aggregates it using a dict.
    """

    usage_per_model: dict[str, LLMUsage] = {}
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: LLMUsage):
        """Track LLM usage, aggregating by model."""
        if usage.is_empty():
            return

        with cls.lock:
            if usage.model not in cls.usage_per_model:
                # First usage for this model
                cls.usage_per_model[usage.model] = usage
            else:
                # Add to existing usage for this model
                cls.usage_per_model[usage.model] += usage

    @classmethod
    def get_total_usage(cls) -> LLMUsage:
        """Get total usage across all models. For backward compatibility."""
        summary = cls.get_usage_summary()
        if not summary:
            return LLMUsage()

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_n_calls = 0
        total_tokens = 0
        total_cost = 0.0

        for usage in summary.values():
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens
            total_tokens += usage.total_tokens
            total_n_calls += usage.n_calls
            total_cost += usage.cost_usd

        return LLMUsage(
            model="multiple",
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            n_calls=total_n_calls,
            cost_usd=total_cost,
        )

    @classmethod
    def get_total_cost(cls) -> float:
        """Get total cost across all models in USD."""
        return cls.get_total_usage().cost_usd

    @classmethod
    def get_usage_summary(cls) -> dict[str, LLMUsage]:
        """Get usage breakdown by model"""
        with cls.lock:
            if not cls.usage_per_model:
                return {}

            # Create a deep copy to avoid modifying tracking data
            summary = {
                model: usage.model_copy(deep=True)
                for model, usage in cls.usage_per_model.items()
            }

            return summary

    @classmethod
    def reset_usage(cls):
        with cls.lock:
            cls.usage_per_model.clear()


class EmbeddingsMetrics(Metrics):
    metrics_queue = Queue()
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: EmbeddingsUsage):
        cls.metrics_queue.put(usage)

    @classmethod
    def get_total_usage(cls) -> EmbeddingsUsage:
        with cls.lock:
            usages = cls.metrics_queue.queue
            if not usages:
                return EmbeddingsUsage()
            model = usages[0].model

            total_usage = sum(
                cls.metrics_queue.queue, start=EmbeddingsUsage(model=model)
            )

            return total_usage


class ContentMetrics(Metrics):
    metrics_queue = Queue()
    lock = Lock()

    @classmethod
    def track_usage(cls, usage: TopicContentTracker):
        cls.metrics_queue.put(usage)

    @classmethod
    def get_total_usage(cls) -> dict[str, TopicContentTracker]:
        with cls.lock:
            usages = cls.metrics_queue.queue
            if not usages:
                return {}
            return TopicContentTracker.aggregate_per_topic(usages)


class StepMetrics:
    """
    Tracks LLM usage, embedding usage, and timing per pipeline step.
    Allows analyzing which steps consume the most resources.
    """

    usage_per_step: dict[str, StepUsage] = {}
    _active_steps: dict[str, float] = {}  # step_name -> start_time
    _current_step: str | None = None  # Currently active step from track_step context
    lock = Lock()

    @classmethod
    def set_current_step(cls, step_name: str | None):
        """Set the currently active step (called by track_step context manager)."""
        with cls.lock:
            cls._current_step = step_name

    @classmethod
    def get_current_step(cls) -> str | None:
        """Get the currently active step name, if any."""
        with cls.lock:
            return cls._current_step

    @classmethod
    def start_step(cls, step_name: str):
        """Start timing a step."""
        with cls.lock:
            cls._active_steps[step_name] = perf_counter()
            if step_name not in cls.usage_per_step:
                cls.usage_per_step[step_name] = StepUsage(step_name=step_name)

    @classmethod
    def end_step(cls, step_name: str):
        """End timing a step and record duration."""
        with cls.lock:
            if step_name in cls._active_steps:
                duration = perf_counter() - cls._active_steps[step_name]
                if step_name in cls.usage_per_step:
                    cls.usage_per_step[step_name].duration_seconds += duration
                del cls._active_steps[step_name]

    @classmethod
    def track_llm_usage(cls, step_name: str, usage: LLMUsage):
        """Track LLM usage for a specific step."""
        if usage.is_empty():
            return

        with cls.lock:
            if step_name not in cls.usage_per_step:
                cls.usage_per_step[step_name] = StepUsage(step_name=step_name)
            
            step = cls.usage_per_step[step_name]
            step.llm_cost_usd += usage.cost_usd
            step.llm_tokens += usage.total_tokens
            step.llm_calls += usage.n_calls

    @classmethod
    def track_embedding_usage(cls, step_name: str, usage: EmbeddingsUsage):
        """Track embedding usage for a specific step."""
        with cls.lock:
            if step_name not in cls.usage_per_step:
                cls.usage_per_step[step_name] = StepUsage(step_name=step_name)
            
            step = cls.usage_per_step[step_name]
            step.embedding_cost_usd += usage.cost_usd
            step.embedding_tokens += usage.tokens

    @classmethod
    def get_step_summary(cls) -> dict[str, dict]:
        """Get summary of all steps with cost and time."""
        with cls.lock:
            return {
                name: step.to_summary_dict()
                for name, step in cls.usage_per_step.items()
            }

    @classmethod
    def get_totals(cls) -> dict:
        """Get total cost and duration across all steps."""
        with cls.lock:
            total_llm_cost = 0.0
            total_embedding_cost = 0.0
            total_duration = 0.0
            total_llm_tokens = 0
            total_embedding_tokens = 0
            total_llm_calls = 0

            for step in cls.usage_per_step.values():
                total_llm_cost += step.llm_cost_usd
                total_embedding_cost += step.embedding_cost_usd
                total_duration += step.duration_seconds
                total_llm_tokens += step.llm_tokens
                total_embedding_tokens += step.embedding_tokens
                total_llm_calls += step.llm_calls

            return {
                "total_llm_cost_usd": round(total_llm_cost, 6),
                "total_embedding_cost_usd": round(total_embedding_cost, 6),
                "total_cost_usd": round(total_llm_cost + total_embedding_cost, 6),
                "total_duration_seconds": round(total_duration, 3),
                "total_llm_tokens": total_llm_tokens,
                "total_embedding_tokens": total_embedding_tokens,
                "total_llm_calls": total_llm_calls,
            }

    @classmethod
    def reset_usage(cls):
        """Reset all step metrics."""
        with cls.lock:
            cls.usage_per_step.clear()
            cls._active_steps.clear()
            cls._current_step = None


@dataclass(frozen=True, slots=True)
class _PipelineWallRecord:
    """One completed :func:`track_step` wall interval (UTC)."""

    pipeline_step: str
    started_at_utc: datetime
    ended_at_utc: datetime


class EntityStepMetrics:
    """
    Per-entity step metrics tracker (instance-based, thread-safe).
    
    Unlike StepMetrics (class-based), this creates a separate instance per entity,
    allowing correct tracking when entities are processed in parallel.
    """

    def __init__(self, entity_name: str):
        self.entity_name = entity_name
        self.usage_per_step: dict[str, StepUsage] = {}
        self._active_steps: dict[str, float] = {}  # step_name -> start_time
        self._current_step: str | None = None
        self.lock = Lock()
        self._pipeline_wall_records: list[_PipelineWallRecord] = []
        self._novelty_substep_wall: dict[str, dict[str, object]] = {}

    def set_current_step(self, step_name: str | None):
        """Set the currently active step (called by track_step context manager)."""
        with self.lock:
            self._current_step = step_name

    def get_current_step(self) -> str | None:
        """Get the currently active step name, if any."""
        with self.lock:
            return self._current_step

    def start_step(self, step_name: str):
        """Start timing a step."""
        with self.lock:
            self._active_steps[step_name] = perf_counter()
            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)

    def end_step(self, step_name: str):
        """End timing a step and record duration."""
        with self.lock:
            if step_name in self._active_steps:
                duration = perf_counter() - self._active_steps[step_name]
                if step_name in self.usage_per_step:
                    self.usage_per_step[step_name].duration_seconds += duration
                del self._active_steps[step_name]

    def record_pipeline_step_wall(
        self,
        pipeline_step: str,
        started_at_utc: datetime,
        ended_at_utc: datetime,
    ) -> None:
        """Append one wall-clock row for a completed :func:`track_step` context."""
        with self.lock:
            self._pipeline_wall_records.append(
                _PipelineWallRecord(
                    pipeline_step=pipeline_step,
                    started_at_utc=started_at_utc,
                    ended_at_utc=ended_at_utc,
                )
            )

    def accumulate_novelty_substep_wall(
        self,
        substep: str,
        started_at_utc: datetime,
        ended_at_utc: datetime,
    ) -> None:
        """Add wall time for a novelty_check substep (may be called multiple times per run)."""
        delta = (ended_at_utc - started_at_utc).total_seconds()
        with self.lock:
            acc = self._novelty_substep_wall.setdefault(
                substep,
                {"duration": 0.0, "first": None, "last": None},
            )
            acc["duration"] = float(acc["duration"]) + delta
            first = acc["first"]
            last = acc["last"]
            if first is None or started_at_utc < first:
                acc["first"] = started_at_utc
            if last is None or ended_at_utc > last:
                acc["last"] = ended_at_utc

    def get_step_wall_timings(self) -> list[dict[str, object]]:
        """Serializable rows for ``step_wall_timings`` in step_metrics.json and SQLite."""
        with self.lock:
            records = list(self._pipeline_wall_records)
            novelty_acc = {k: dict(v) for k, v in self._novelty_substep_wall.items()}

        def _iso(dt: datetime) -> str:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        rows: list[dict[str, object]] = []
        for rec in records:
            dur = (rec.ended_at_utc - rec.started_at_utc).total_seconds()
            rows.append(
                {
                    "pipeline_step": rec.pipeline_step,
                    "substep": None,
                    "started_at_utc": _iso(rec.started_at_utc),
                    "ended_at_utc": _iso(rec.ended_at_utc),
                    "duration_seconds": round(dur, 6),
                }
            )

        for substep, acc in novelty_acc.items():
            duration = float(acc.get("duration", 0.0))
            if duration <= 0.0:
                continue
            first = acc.get("first")
            last = acc.get("last")
            if not isinstance(first, datetime) or not isinstance(last, datetime):
                continue
            rows.append(
                {
                    "pipeline_step": "novelty_check",
                    "substep": substep,
                    "started_at_utc": _iso(first),
                    "ended_at_utc": _iso(last),
                    "duration_seconds": round(duration, 6),
                }
            )
        return rows

    def get_step_wall_timings_for_db(self) -> list[dict[str, object]]:
        """Like :meth:`get_step_wall_timings` but with timezone-aware datetimes for ORM rows."""
        with self.lock:
            records = list(self._pipeline_wall_records)
            novelty_acc = {k: dict(v) for k, v in self._novelty_substep_wall.items()}

        def _utc(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        rows: list[dict[str, object]] = []
        for rec in records:
            s = _utc(rec.started_at_utc)
            e = _utc(rec.ended_at_utc)
            dur = (e - s).total_seconds()
            rows.append(
                {
                    "pipeline_step": rec.pipeline_step,
                    "substep": None,
                    "started_at_utc": s,
                    "ended_at_utc": e,
                    "duration_seconds": round(dur, 6),
                }
            )

        for substep, acc in novelty_acc.items():
            duration = float(acc.get("duration", 0.0))
            if duration <= 0.0:
                continue
            first = acc.get("first")
            last = acc.get("last")
            if not isinstance(first, datetime) or not isinstance(last, datetime):
                continue
            f = _utc(first)
            end_u = _utc(last)
            rows.append(
                {
                    "pipeline_step": "novelty_check",
                    "substep": substep,
                    "started_at_utc": f,
                    "ended_at_utc": end_u,
                    "duration_seconds": round(duration, 6),
                }
            )
        return rows

    def track_llm_usage(self, usage: LLMUsage, *, attributee_step: str | None = None) -> None:
        """Track LLM usage for ``attributee_step`` or, if omitted, the current :func:`track_step` step."""
        if usage.is_empty():
            return

        with self.lock:
            step_name = attributee_step if attributee_step else self._current_step
            if not step_name:
                return

            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)

            step = self.usage_per_step[step_name]
            step.llm_cost_usd += usage.cost_usd
            step.llm_tokens += usage.total_tokens
            step.llm_calls += usage.n_calls

    def track_embedding_usage(self, usage: EmbeddingsUsage):
        """Track embedding usage for the current step."""
        with self.lock:
            step_name = self._current_step
            if not step_name:
                return
            
            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)
            
            step = self.usage_per_step[step_name]
            step.embedding_cost_usd += usage.cost_usd
            step.embedding_tokens += usage.tokens

    def track_chunks(self, count: int):
        """Track chunks retrieved for the current step."""
        with self.lock:
            step_name = self._current_step
            if not step_name:
                return
            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)
            self.usage_per_step[step_name].chunks_retrieved += count

    def track_bullets(
        self,
        generated: int = 0,
        discarded: int = 0,
        kept: int = 0,
        merged: int = 0,
        rewritten: int = 0,
    ):
        """Track bullet point operations for the current step."""
        with self.lock:
            step_name = self._current_step
            if not step_name:
                return
            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)
            step = self.usage_per_step[step_name]
            step.bullets_generated += generated
            step.bullets_discarded += discarded
            step.bullets_kept += kept
            step.bullets_merged += merged
            step.bullets_rewritten += rewritten

    def track_concepts_themes(self, concepts: int = 0, themes: int = 0):
        """Track concepts and themes extracted for the current step."""
        with self.lock:
            step_name = self._current_step
            if not step_name:
                return
            if step_name not in self.usage_per_step:
                self.usage_per_step[step_name] = StepUsage(step_name=step_name)
            step = self.usage_per_step[step_name]
            step.concepts_count += concepts
            step.themes_count += themes

    def get_step_summary(self) -> dict[str, dict]:
        """Get summary of all steps with cost and time."""
        with self.lock:
            return {
                name: step.to_summary_dict()
                for name, step in self.usage_per_step.items()
            }

    def get_totals(self) -> dict:
        """Get total cost and duration across all steps."""
        with self.lock:
            total_llm_cost = 0.0
            total_embedding_cost = 0.0
            total_duration = 0.0
            total_llm_tokens = 0
            total_embedding_tokens = 0
            total_llm_calls = 0

            for step in self.usage_per_step.values():
                total_llm_cost += step.llm_cost_usd
                total_embedding_cost += step.embedding_cost_usd
                total_duration += step.duration_seconds
                total_llm_tokens += step.llm_tokens
                total_embedding_tokens += step.embedding_tokens
                total_llm_calls += step.llm_calls

            return {
                "total_llm_cost_usd": round(total_llm_cost, 6),
                "total_embedding_cost_usd": round(total_embedding_cost, 6),
                "total_cost_usd": round(total_llm_cost + total_embedding_cost, 6),
                "total_duration_seconds": round(total_duration, 3),
                "total_llm_tokens": total_llm_tokens,
                "total_embedding_tokens": total_embedding_tokens,
                "total_llm_calls": total_llm_calls,
            }

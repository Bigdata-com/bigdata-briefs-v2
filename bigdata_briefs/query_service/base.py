from abc import ABC, abstractmethod
from concurrent.futures.thread import ThreadPoolExecutor

from bigdata_briefs.models import (
    Entity,
    ReportDates,
    Result,
)
from bigdata_briefs.utils import (
    log_args,
    log_performance,
    log_return_value,
)


class BaseQueryService(ABC):
    @abstractmethod
    def cleanup(self):
        pass

    @abstractmethod
    def get_entities(self, entity_ids: list[str]) -> list[Entity]: ...

    @abstractmethod
    @log_performance
    def check_if_entity_has_results(
        self,
        entity_id: str,
        report_dates: ReportDates,
        similarity_text: str | None = None,
        *,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = None,
        chunk_limit: int | None = None,
        rerank_threshold: float = 0.0,
    ) -> list[Result]: ...

    @abstractmethod
    @log_performance
    def _run_single_exploratory_search(
        self,
        entity_id: str,
        report_dates: ReportDates,
        similarity_text: str | None = None,
        topic: str | None = None,
        *,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = None,
        chunk_limit: int | None = None,
        use_topics: bool = True,
        rerank_threshold: float | None = None,
        source_rank_boost: int | None,
        freshness_boost: int | None,
    ): ...

    @abstractmethod
    @log_performance
    @log_args
    @log_return_value
    def run_exploratory_search(
        self,
        entity: Entity,
        topics: list[str],
        report_dates: ReportDates,
        executor: ThreadPoolExecutor,
        *,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = None,
        chunk_limit: int | None = None,
        use_topics: bool = True,
        rerank_threshold: float | None = None,
        source_rank_boost: int | None,
        freshness_boost: int | None,
    ) -> list[Result]: ...

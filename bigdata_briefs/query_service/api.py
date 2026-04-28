from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Semaphore
from uuid import UUID

import httpx

from bigdata_briefs import logger
from bigdata_briefs.debug_logger import DebugLogger
from bigdata_briefs.exceptions import TooManyAPIRetriesError
from bigdata_briefs.metrics import ContentMetrics, QueryUnitMetrics
from bigdata_briefs.models import (
    ConceptExtraction,
    Entity,
    ReportDates,
    Result,
    TopicContentTracker,
)
from bigdata_briefs.novelty.chunk_filter import ChunkFilterService
from bigdata_briefs.query_service.base import BaseQueryService
from bigdata_briefs.query_service.models import SearchAPIQueryDict
from bigdata_briefs.query_service.rate_limit import RequestsPerMinuteController
from bigdata_briefs.settings import settings
from bigdata_briefs.utils import (
    log_args,
    log_performance,
    log_return_value,
    log_time,
    sleep_with_backoff,
)


def _get_date_range_from_payload(payload: dict) -> str:
    """Extract date range from query payload for debug logging."""
    try:
        timestamp = payload.get("query", payload).get("filters", {}).get("timestamp", {})
        if not timestamp:
            return ""
        start_str = timestamp.get("start", "")[:10]  # Get YYYY-MM-DD only
        end_str = timestamp.get("end", "")[:10]
        if not start_str or not end_str:
            return ""
        # Parse dates to check if single day
        start_date = datetime.fromisoformat(start_str)
        end_date = datetime.fromisoformat(end_str)
        if start_date.date() == end_date.date():
            return f" [{start_str}]"
        return f" [{start_str} to {end_str}]"
    except Exception:
        return ""

MAX_REQUESTS_PER_MINUTE = (
    460  # Backend rate limit (500 max, using 460 for maximum safety margin)
)
REFRESH_FREQUENCY_RATE_LIMIT = 5  # Time in seconds to pro-rate the rate limiter, lower values = smoother requests, more overhead
TIME_BEFORE_RETRY_RATE_LIMITER = 1.0  # Time in seconds before retrying the request

MAX_ENTITIES_PER_KG_ENTITY_BY_ID_REQUEST = (
    100  # Max number of entities per request to /v1/knowledge-graph/entities/id
)


class APIQueryService(BaseQueryService):
    def __init__(
        self,
        debug_logger: DebugLogger | None = None,
        chunk_filter_service: ChunkFilterService | None = None,
        *,
        rate_limiter: RequestsPerMinuteController | None = None,
        connection_sem: Semaphore | None = None,
        http_client: httpx.Client | None = None,
    ):
        """
        Parallel-entity support: when ``rate_limiter``, ``connection_sem`` and
        ``http_client`` are provided (by the FastAPI lifespan), every
        ``APIQueryService`` across concurrent runs shares the same 450 QPM
        budget, connection pool, and HTTP client. CLI / legacy callers pass
        nothing and get per-run locals like before.
        """
        self._api_key = settings.BIGDATA_API_KEY
        # Max number of concurrent connections to the SDK. Shared when injected.
        self.semaphore = connection_sem or Semaphore(
            value=settings.API_SIMULTANEOUS_REQUESTS
        )
        self._client = http_client or httpx.Client(
            base_url=settings.API_BASE_URL,
            headers=self.headers,
            timeout=settings.API_TIMEOUT_SECONDS,
        )
        # Only close the client in cleanup() if we own it (created it locally).
        self._owns_client = http_client is None
        self.rate_limit_controller = rate_limiter or RequestsPerMinuteController(
            max_requests_per_min=MAX_REQUESTS_PER_MINUTE,
            rate_limit_refresh_frequency=REFRESH_FREQUENCY_RATE_LIMIT,
            seconds_before_retry=TIME_BEFORE_RETRY_RATE_LIMITER,
        )
        self.debug_logger = debug_logger
        self.chunk_filter_service = chunk_filter_service

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._api_key, "Content-Type": "application/json"}

    def cleanup(self):
        if self._owns_client:
            self._client.close()

    def get_entities(self, entity_ids: list[str]) -> list[Entity]:
        # Batch entity_ids into chunks
        def batched(iterable, n):
            for i in range(0, len(iterable), n):
                yield iterable[i : i + n]

        entities = []
        for batch in batched(entity_ids, MAX_ENTITIES_PER_KG_ENTITY_BY_ID_REQUEST):
            raw_entities = self._call_api(
                endpoint="/v1/knowledge-graph/entities/id",
                method="POST",
                payload={"values": batch},
                headers=self.headers,
            )
            for entity_data in raw_entities["results"].values():
                entities.append(Entity.from_api(entity_data))

        return entities

    @log_args
    @log_return_value
    @log_time
    def api_search(
        self,
        endpoint: str,
        method: str,
        payload: dict,
        step: str = "api_search",
        entity_id: str | None = None,
        debug_logger: "DebugLogger | None" = None,
    ):
        results = self._call_api(endpoint, method, payload, self.headers, step=step, entity_id=entity_id, debug_logger=debug_logger)

        QueryUnitMetrics.track_usage(results["usage"]["api_query_units"])

        parsed_results = []
        for result in results["results"]:
            parsed_results.append(Result.from_api(result))

        # Terminal log
        n_results = len(parsed_results)
        date_range = _get_date_range_from_payload(payload)
        logger.info(f"Query API: {step} for entity {entity_id or 'N/A'}{date_range} → {n_results} results")

        return parsed_results

    def _call_api(
        self,
        endpoint: str,
        method: str,
        payload: dict,
        headers: dict,
        step: str = "api_call",
        entity_id: str | None = None,
        debug_logger: "DebugLogger | None" = None,
    ) -> dict:
        with self.semaphore:
            for attempt in range(settings.API_RETRIES):
                try:
                    result = self.rate_limit_controller(
                        self._client.request,
                        method=method,
                        url=endpoint,
                        json=payload,
                        headers=headers,
                    )
                    result.raise_for_status()
                    response_json = result.json()
                    
                    # Save to debug logger if available (use passed debug_logger or fall back to self.debug_logger)
                    effective_logger = debug_logger or self.debug_logger
                    if effective_logger:
                        effective_logger.save_query_api(
                            step=step,
                            entity_id=entity_id,
                            endpoint=endpoint,
                            method=method,
                            payload=payload,
                            response=response_json,
                            status_code=result.status_code,
                        )
                    
                    # Terminal log
                    n_results = len(response_json.get("results", []))
                    date_range = _get_date_range_from_payload(payload)
                    logger.info(f"Query API: {step} ({endpoint}){date_range} → {n_results} results, status {result.status_code}")
                    
                    return response_json
                except (httpx.HTTPStatusError, httpx.ConnectTimeout) as e:
                    last_exception = e
                    logger.warning(
                        f"Error calling API {method} at endpoint {endpoint}: {e}. Attempt {attempt + 1}"
                    )
                    sleep_with_backoff(attempt=attempt)

        msg = f"Too many API retries for {method} at endpoint {endpoint}. Last error {last_exception}."
        if isinstance(last_exception, httpx.HTTPStatusError):
            msg = msg + f" Response body {last_exception.response.text}"
        raise TooManyAPIRetriesError(msg)

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
        chunk_limit: int | None = 1,
        rerank_threshold: float | None = None,
        debug_logger: "DebugLogger | None" = None,
    ) -> list[Result]:
        """
        Make a simple query to find if the entity has results.
        Based on this, the next steps will happen or not.
        """

        query = build_query(
            entity_id=entity_id,
            report_dates=report_dates,
            similarity_text=similarity_text,
            source_filter=source_filter,
            categories=categories,
            sentiment_threshold=sentiment_threshold,
            chunk_limit=chunk_limit,
            rerank_threshold=rerank_threshold,
            source_rank_boost=None,
            freshness_boost=None,
        )
        results = self.api_search(
            endpoint="/v1/search",
            method="POST",
            payload=query,
            step="initial_check",
            entity_id=entity_id,
            debug_logger=debug_logger,
        )

        ContentMetrics.track_usage(
            TopicContentTracker(
                topic="Check if entity has results",
                retrieval=TopicContentTracker.retrieval_from_sdk_result(
                    sdk_results=results,
                    entity_id=entity_id,
                ),
            )
        )

        return results

    @log_performance
    def _run_single_exploratory_search(
        self,
        entity_id: str,
        report_dates: ReportDates,
        similarity_text: str | None = None,
        topic: str | None = None,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = settings.EXPLORATORY_SENTIMENT_THRESHOLD,
        chunk_limit: int | None = settings.API_CHUNKS_LIMIT_EXPLORATORY,
        rerank_threshold: float | None = settings.API_RERANK_EXPLORATORY,
        source_rank_boost: int | None = settings.API_SOURCE_RANK_BOOST,
        freshness_boost: int | None = settings.API_FRESHNESS_BOOST,
        topic_index: int | None = None,
        debug_logger: "DebugLogger | None" = None,
    ):
        query = build_query(
            entity_id=entity_id,
            report_dates=report_dates,
            similarity_text=similarity_text,
            source_filter=source_filter,
            categories=categories,
            sentiment_threshold=sentiment_threshold,
            chunk_limit=chunk_limit,
            rerank_threshold=rerank_threshold,
            source_rank_boost=source_rank_boost,
            freshness_boost=freshness_boost,
        )

        step_name = f"exploratory_{topic_index}" if topic_index is not None else "exploratory"
        if topic:
            step_name = f"exploratory_topic_{topic_index}" if topic_index is not None else "exploratory_topic"

        results = self.api_search(
            endpoint="/v1/search",
            method="POST",
            payload=query,
            step=step_name,
            entity_id=entity_id,
            debug_logger=debug_logger,
        )

        if topic:
            ContentMetrics.track_usage(
                TopicContentTracker(
                    topic=topic,
                    retrieval=TopicContentTracker.retrieval_from_sdk_result(
                        sdk_results=results,
                        entity_id=entity_id,
                    ),
                )
            )

        return results

    @log_performance
    @log_args
    @log_return_value
    def run_exploratory_search(
        self,
        entity: Entity,
        report_dates: ReportDates,
        executor: ThreadPoolExecutor,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = settings.EXPLORATORY_SENTIMENT_THRESHOLD,
        chunk_limit: int | None = settings.API_CHUNKS_LIMIT_EXPLORATORY,
        rerank_threshold: float | None = settings.API_RERANK_EXPLORATORY,
        source_rank_boost: int | None = settings.API_SOURCE_RANK_BOOST,
        freshness_boost: int | None = settings.API_FRESHNESS_BOOST,
        debug_logger: "DebugLogger | None" = None,
    ) -> list[Result]:
        logger.info(
            f"Exploratory search for {entity.name}: chunk_limit={chunk_limit}"
        )
        return self._run_single_exploratory_search(
            entity_id=entity.id,
            report_dates=report_dates,
            source_filter=source_filter,
            categories=categories,
            sentiment_threshold=sentiment_threshold,
            chunk_limit=chunk_limit,
            rerank_threshold=rerank_threshold,
            source_rank_boost=source_rank_boost,
            freshness_boost=freshness_boost,
            topic_index=0,
            debug_logger=debug_logger,
        )

    def _run_concept_single_query(
        self,
        entity_id: str,
        entity_name: str,
        concept: str,
        report_dates: ReportDates,
        *,
        theme: str | None = None,
        source_filter: list[str] | None = None,
        categories: list[str] | None = None,
        sentiment_threshold: float | None = settings.FOLLOWUP_SENTIMENT_THRESHOLD,
        chunk_limit: int | None = settings.API_CHUNK_LIMIT_FOLLOWUP,
        rerank_threshold: float | None = settings.API_RERANK_EXPLORATORY,  # 0.8 for concept search
        source_rank_boost: int | None = settings.API_SOURCE_RANK_BOOST,
        freshness_boost: int | None = settings.API_FRESHNESS_BOOST,
        concept_index: int | None = None,
        debug_logger: "DebugLogger | None" = None,
        headline_search: bool = False,
    ):
        """Run a single concept-based query: "{entity_name} {concept}"."""
        similarity_text = f"{entity_name} {concept}"
        
        query = build_query(
            entity_id=entity_id,
            report_dates=report_dates,
            similarity_text=similarity_text,
            source_filter=source_filter,
            categories=categories,
            sentiment_threshold=sentiment_threshold,
            chunk_limit=chunk_limit,
            rerank_threshold=rerank_threshold,
            source_rank_boost=source_rank_boost,
            freshness_boost=freshness_boost,
            headline_search=headline_search,
        )

        # Build step name with theme and concept for better debug logging
        if theme and concept_index is not None:
            step_name = f"{theme}: {concept} - {concept_index}"
        elif concept_index is not None:
            step_name = f"concept_{concept_index}"
        else:
            step_name = "concept"
        results = self.api_search(
            endpoint="/v1/search",
            method="POST",
            payload=query,
            step=step_name,
            entity_id=entity_id,
            debug_logger=debug_logger,
        )

        ContentMetrics.track_usage(
            TopicContentTracker(
                topic=f"Concept search: {concept}",
                retrieval=TopicContentTracker.retrieval_from_sdk_result(
                    sdk_results=results,
                    entity_id=entity_id,
                ),
            )
        )

        return results

    def run_concept_queries_raw(
        self,
        entity: Entity,
        concepts: ConceptExtraction,
        report_dates: ReportDates,
        source_filter: list[str] | None,
        categories: list[str] | None,
        executor: ThreadPoolExecutor,
        chunk_limit: int | None = None,
        source_rank_boost: int | None = settings.API_SOURCE_RANK_BOOST,
        freshness_boost: int | None = settings.API_FRESHNESS_BOOST,
        rerank_concept_sources: bool = False,
        debug_logger: "DebugLogger | None" = None,
        headline_search: bool = False,
    ) -> tuple[list[Result], dict[str, dict], dict[str, list[Result]]]:
        """
        Step 4A: Run raw API queries for each concept.
        
        This method only performs the API calls and collects raw results.
        It does NOT do deduplication, hash filtering, or reranking.
        
        Args:
            entity: The entity to search for
            concepts: Extracted concepts with categories
            report_dates: Date range for the search
            source_filter: Optional list of sources to filter
            categories: Optional list of categories to filter
            executor: ThreadPoolExecutor for parallel queries
            chunk_limit: Max chunks per query
            source_rank_boost: Boost for source ranking
            freshness_boost: Boost for freshness
            rerank_concept_sources: Whether reranking will be applied (affects chunk_limit)
            debug_logger: Optional debug logger
            headline_search: Whether to search headlines
            
        Returns:
            Tuple of:
            - all_results: Raw list of all Result objects
            - results_per_concept: Dict mapping concept -> {theme, results}
            - results_by_theme: Dict mapping theme -> [results] (raw, not deduplicated)
        """
        # Determine chunk_limit and rerank_threshold based on rerank mode
        if rerank_concept_sources:
            chunk_limit = settings.RERANK_CONCEPT_CHUNK_LIMIT  # 45
            rerank_threshold = settings.RERANK_CONCEPT_THRESHOLD  # 0.7
        else:
            if chunk_limit is None:
                chunk_limit = settings.API_CHUNK_LIMIT_FOLLOWUP  # 15
            rerank_threshold = settings.API_RERANK_EXPLORATORY  # 0.8
        
        # Flatten all concepts from all categories, keeping track of category
        all_concepts_with_category = []
        for category in concepts.categories:
            for concept in category.concepts:
                all_concepts_with_category.append({
                    "theme": category.theme,
                    "concept": concept
                })
        
        logger.info(
            f"Running concept-based search for {entity.name}: "
            f"{len(all_concepts_with_category)} concepts, chunk_limit={chunk_limit} per concept"
        )
        
        # Submit all concept queries in parallel
        future_to_concept_info = {
            executor.submit(
                self._run_concept_single_query,
                entity_id=entity.id,
                entity_name=entity.name,
                concept=concept_info["concept"],
                report_dates=report_dates,
                theme=concept_info["theme"],
                source_filter=source_filter,
                categories=categories,
                chunk_limit=chunk_limit,
                rerank_threshold=rerank_threshold,
                source_rank_boost=source_rank_boost,
                freshness_boost=freshness_boost,
                concept_index=idx,
                debug_logger=debug_logger,
                headline_search=headline_search,
            ): concept_info
            for idx, concept_info in enumerate(all_concepts_with_category)
        }
        
        # Collect results per concept for debug logging
        results_per_concept: dict[str, dict] = {}  # {concept: {theme, results}}
        results_by_theme: dict[str, list[Result]] = {}  # {theme: [results]}
        all_results: list[Result] = []
        
        for future in as_completed(future_to_concept_info):
            concept_info = future_to_concept_info[future]
            concept = concept_info["concept"]
            theme = concept_info["theme"]
            
            try:
                results = future.result()
                all_results.extend(results)
                
                # Store results for this concept
                results_per_concept[concept] = {
                    "theme": theme,
                    "results": results
                }
                
                # Group results by theme
                if theme not in results_by_theme:
                    results_by_theme[theme] = []
                results_by_theme[theme].extend(results)
            except Exception as e:
                logger.warning(
                    f"Error running concept query for '{concept}' on entity {entity.id}: {e}"
                )
        
        return all_results, results_per_concept, results_by_theme

    def process_concept_results(
        self,
        entity: Entity,
        concepts: ConceptExtraction,
        all_results: list[Result],
        results_per_concept: dict[str, dict],
        results_by_theme: dict[str, list[Result]],
        report_dates: ReportDates,
        rerank_concept_sources: bool = False,
        store_retrieved_chunks: bool | None = None,
        debug_logger: "DebugLogger | None" = None,
    ) -> tuple[list[Result], dict[str, list[Result]]]:
        """
        Step 4B: Process raw concept results.
        
        Performs:
        1. Deduplication by doc_id + chunk_num
        2. Hash filtering (removes already-used chunks)
        3. Hash storage (saves new chunk hashes)
        4. Optional reranking by source_rank
        5. Theme grouping and debug logging
        
        Args:
            entity: The entity being processed
            concepts: Extracted concepts with categories
            all_results: Raw results from run_concept_queries_raw
            results_per_concept: Results grouped by concept
            results_by_theme: Results grouped by theme (raw)
            report_dates: Date range for lookback
            rerank_concept_sources: Whether to apply reranking
            store_retrieved_chunks: Whether to filter/store chunk hashes
            debug_logger: Optional debug logger
            
        Returns:
            Tuple of:
            - deduplicated: Deduplicated and filtered Result list
            - results_by_theme: Processed results grouped by theme
        """
        # Deduplicate based on document_id-chunk_id
        deduplicated = self._deduplicate_results(all_results)

        logger.info(
            f"Concept search results: {len(all_results)} total → "
            f"{len(deduplicated)} after deduplication"
        )

        # Apply per-theme reranking if enabled
        if rerank_concept_sources:
            deduplicated, results_by_theme = self._rerank_by_theme(
                concepts,
                results_per_concept,
                settings.RERANK_CONCEPT_LIMIT_PER_CONCEPT
            )

        # Keep only rank-1 and rank-2 sources — applied AFTER reranking so this
        # filter is always the final gate regardless of which code path was taken.
        before_rank_filter = len(deduplicated)
        deduplicated = [r for r in deduplicated if r.source_rank in (1, 2)]
        results_by_theme = {
            theme: [r for r in items if r.source_rank in (1, 2)]
            for theme, items in results_by_theme.items()
        }
        logger.info(
            f"Concept search: {before_rank_filter} → {len(deduplicated)} after rank filter (rank 1-2 only)"
        )
        
        # Filter chunks by hash (already-used content across runs)
        use_chunk_filter = store_retrieved_chunks if store_retrieved_chunks is not None else settings.STORE_RETRIEVED_CHUNKS
        if use_chunk_filter and self.chunk_filter_service:
            from datetime import timedelta
            
            # Calculate lookback period - EXCLUDE current day to allow re-runs
            # Lookback: from (today - 14 days) to (today - 1 day)
            lookback_start = report_dates.end - timedelta(days=settings.CHUNK_HASH_LOOKBACK_DAYS)
            lookback_end = report_dates.end - timedelta(days=1)  # Exclude current day
            
            deduplicated, results_by_theme, removed_count, stored_count, removed_details = self.chunk_filter_service.filter_and_store_chunks(
                deduplicated,
                results_by_theme,
                entity.id,
                start_date=lookback_start,
                end_date=lookback_end,  # Exclude current day
                current_date=report_dates.end,
            )
            
            # Log to debug logger if available
            effective_logger = debug_logger or self.debug_logger
            if effective_logger:
                remaining_chunks = sum(len(r.chunks) for r in deduplicated)
                effective_logger.save_chunk_hash_filter_stats(
                    entity.name, removed_count, stored_count, remaining_chunks, removed_details
                )
        
        # Calculate and log source rank distribution
        effective_logger = debug_logger or self.debug_logger
        if effective_logger:
            rank_dist, total_chunks, source_breakdown = self._calculate_source_rank_distribution(deduplicated)
            effective_logger.save_source_rank_distribution(
                entity.name, rank_dist, total_chunks, source_breakdown
            )
        
        # Check for text duplicates within concepts (same text, different document_id)
        if effective_logger:
            duplicates_by_concept, total_dups = self._find_text_duplicates_by_concept(
                results_per_concept
            )
            if total_dups > 0:
                effective_logger.save_text_duplicates_within_concepts(
                    entity.name, duplicates_by_concept, total_dups
                )
        
        # Store concept search data for later (will be saved with bullet points)
        if effective_logger:
            self._last_concepts_data = self._build_concepts_data(concepts, results_per_concept)
            self._last_entity_name = entity.name
            # Also save intermediate summary
            effective_logger.save_concept_search_summary(entity.name, self._last_concepts_data)
        
        return deduplicated, results_by_theme

    @log_performance
    def run_query_with_concepts(
        self,
        entity: Entity,
        concepts: ConceptExtraction,
        report_dates: ReportDates,
        source_filter: list[str] | None,
        categories: list[str] | None,
        executor: ThreadPoolExecutor,
        chunk_limit: int | None = None,
        source_rank_boost: int | None = settings.API_SOURCE_RANK_BOOST,
        freshness_boost: int | None = settings.API_FRESHNESS_BOOST,
        rerank_concept_sources: bool = False,
        debug_logger: "DebugLogger | None" = None,
        headline_search: bool = False,
        store_retrieved_chunks: bool | None = None,
    ) -> tuple[list[Result], dict[str, list[Result]]]:
        """
        Run queries for each concept in the ConceptExtraction.
        For each concept: query "{entity} {concept}".
        
        This is a convenience method that runs both Step 4A and Step 4B together
        for backwards compatibility.
        
        When rerank_concept_sources=True:
            - Fetches more chunks (45) with lower threshold (0.7)
            - Re-ranks by source_rank then relevance
            - Keeps top 15 per concept
        
        Returns:
            - deduplicated results based on document_id-chunk_id (all results)
            - results grouped by theme (for split_concept_calls mode)
        """
        # Step 4A: Run raw queries
        all_results, results_per_concept, results_by_theme = self.run_concept_queries_raw(
            entity=entity,
            concepts=concepts,
            report_dates=report_dates,
            source_filter=source_filter,
            categories=categories,
            executor=executor,
            chunk_limit=chunk_limit,
            source_rank_boost=source_rank_boost,
            freshness_boost=freshness_boost,
            rerank_concept_sources=rerank_concept_sources,
            debug_logger=debug_logger,
            headline_search=headline_search,
        )
        
        # Step 4B: Process results
        deduplicated, processed_by_theme = self.process_concept_results(
            entity=entity,
            concepts=concepts,
            all_results=all_results,
            results_per_concept=results_per_concept,
            results_by_theme=results_by_theme,
            report_dates=report_dates,
            rerank_concept_sources=rerank_concept_sources,
            store_retrieved_chunks=store_retrieved_chunks,
            debug_logger=debug_logger,
        )
        
        return deduplicated, processed_by_theme

    def _build_concepts_data(
        self,
        concepts: ConceptExtraction,
        results_per_concept: dict[str, dict],
    ) -> list[dict]:
        """Build concept search data structure."""
        concepts_data = []
        
        for category in concepts.categories:
            category_data = {
                "theme": category.theme,
                "concepts": category.concepts,
                "results": []
            }
            
            for concept in category.concepts:
                concept_results = results_per_concept.get(concept, {}).get("results", [])
                
                # Build chunk data with only the fields we need
                chunks_data = []
                for result in concept_results:
                    for chunk in result.chunks:
                        chunks_data.append({
                            "document_id": result.document_id,
                            "headline": result.headline,
                            "ts": result.ts,
                            "source_name": result.source_name,
                            "source_rank": result.source_rank,
                            "text": chunk.text,
                            "chunk_id": chunk.chunk,
                        })
                
                category_data["results"].append({
                    "concept": concept,
                    "chunks": chunks_data
                })
            
            concepts_data.append(category_data)
        
        return concepts_data
    
    def get_last_concepts_data(self) -> tuple[str, list[dict]] | None:
        """Get the last concept search data for saving with bullet points."""
        if hasattr(self, '_last_concepts_data') and hasattr(self, '_last_entity_name'):
            return self._last_entity_name, self._last_concepts_data
        return None

    def _deduplicate_results(self, results: list[Result]) -> list[Result]:
        """
        Deduplicate results based on document_id-chunk_id.
        Keeps the first occurrence of each unique chunk.
        """
        seen_chunks: set[str] = set()
        deduplicated: list[Result] = []
        
        for result in results:
            # Filter chunks that haven't been seen yet
            new_chunks = []
            for chunk in result.chunks:
                chunk_key = f"{result.document_id}-{chunk.chunk}"
                if chunk_key not in seen_chunks:
                    seen_chunks.add(chunk_key)
                    new_chunks.append(chunk)
            
            # Only include result if it has any new chunks
            if new_chunks:
                # Create a new Result with only the new chunks
                deduplicated_result = Result(
                    document_id=result.document_id,
                    headline=result.headline,
                    timestamp=result.timestamp,
                    source_key=result.source_key,
                    source_name=result.source_name,
                    source_rank=result.source_rank,
                    url=result.url,
                    ts=result.ts,
                    document_scope=result.document_scope,
                    language=result.language,
                    chunks=tuple(new_chunks),
                )
                deduplicated.append(deduplicated_result)
        
        return deduplicated

    def _find_text_duplicates_by_concept(
        self,
        results_per_concept: dict[str, dict],
    ) -> tuple[dict[str, list[dict]], int]:
        """
        Find chunks with identical text but different document_id within each concept.
        
        These are chunks that passed document_id+chunk_num deduplication but have
        the same text content from different source documents (e.g., syndicated articles).
        
        Returns:
            - duplicates_by_concept: {concept: [{text_preview, occurrences: [{doc_id, chunk_num, headline, source}]}]}
            - total_duplicate_count: Total number of duplicate occurrences
        """
        duplicates_by_concept: dict[str, list[dict]] = {}
        total_duplicates = 0
        
        for concept, concept_data in results_per_concept.items():
            results = concept_data.get("results", [])
            
            # Group chunks by text
            text_to_occurrences: dict[str, list[dict]] = {}
            for result in results:
                for chunk in result.chunks:
                    text = chunk.text.strip()
                    if text not in text_to_occurrences:
                        text_to_occurrences[text] = []
                    text_to_occurrences[text].append({
                        "doc_id": result.document_id,
                        "chunk_num": chunk.chunk,
                        "headline": result.headline,
                        "source": result.source_name,
                    })
            
            # Find texts that appear in multiple different documents
            concept_duplicates = []
            for text, occurrences in text_to_occurrences.items():
                # Check if same text appears in different documents
                unique_doc_ids = set(occ["doc_id"] for occ in occurrences)
                if len(unique_doc_ids) > 1:
                    # This text appears in multiple different documents
                    concept_duplicates.append({
                        "text_preview": text[:150] + "..." if len(text) > 150 else text,
                        "occurrence_count": len(occurrences),
                        "unique_documents": len(unique_doc_ids),
                        "occurrences": occurrences,
                    })
                    total_duplicates += len(occurrences)
            
            if concept_duplicates:
                duplicates_by_concept[concept] = concept_duplicates
        
        return duplicates_by_concept, total_duplicates

    def _rerank_by_theme(
        self,
        concepts: ConceptExtraction,
        results_per_concept: dict[str, dict],
        limit_per_concept: int = 15
    ) -> tuple[list[Result], dict[str, list[Result]]]:
        """
        Re-rank chunks per-theme: only apply limiting if theme has too many chunks.
        
        For each theme:
        1. Count unique chunks across all concepts of that theme
        2. Calculate limit: num_concepts_in_theme * limit_per_concept
        3. If theme_chunks > limit: apply per-concept limiting for that theme
        4. Else: keep all chunks for that theme
        
        Args:
            concepts: ConceptExtraction with categories
            results_per_concept: {concept: {theme, results: [Result]}}
            limit_per_concept: Maximum chunks per concept when limiting (default 15)
            
        Returns:
            - deduplicated results
            - results grouped by theme
        """
        # Helper to parse source_rank
        def parse_rank(rank_str: int | None) -> int:
            if rank_str is None:
                return 999
            return rank_str
        
        results_by_theme: dict[str, list[Result]] = {}
        all_theme_results: list[Result] = []
        
        for category in concepts.categories:
            theme = category.theme
            theme_concepts = category.concepts
            limit_for_theme = len(theme_concepts) * limit_per_concept
            
            # Collect all chunks for this theme (across all its concepts)
            theme_chunk_entries = []  # [(result, chunk, relevance, rank)]
            for concept in theme_concepts:
                if concept in results_per_concept:
                    for result in results_per_concept[concept]["results"]:
                        for chunk in result.chunks:
                            theme_chunk_entries.append({
                                "result": result,
                                "chunk": chunk,
                                "relevance": chunk.relevance or 0.0,
                                "rank": parse_rank(result.source_rank),
                                "concept": concept,
                            })
            
            # Count unique chunks for this theme
            unique_chunk_keys = set(
                f"{e['result'].document_id}-{e['chunk'].chunk}" 
                for e in theme_chunk_entries
            )
            theme_unique_chunks = len(unique_chunk_keys)
            
            if theme_unique_chunks > limit_for_theme:
                # Apply per-concept limiting for this theme
                logger.info(
                    f"Theme '{theme}': {theme_unique_chunks} chunks > {limit_for_theme} limit "
                    f"({len(theme_concepts)} concepts × {limit_per_concept}) → reranking"
                )
                
                # Group entries by concept, rerank each, take top N
                theme_results = self._rerank_theme_concepts(
                    theme, theme_concepts, results_per_concept, limit_per_concept, parse_rank
                )
            else:
                # Keep all chunks for this theme (no reranking)
                logger.info(
                    f"Theme '{theme}': {theme_unique_chunks} chunks ≤ {limit_for_theme} limit → keeping all"
                )
                # Collect all unique results for this theme
                theme_results = []
                seen_docs = set()
                for concept in theme_concepts:
                    if concept in results_per_concept:
                        for result in results_per_concept[concept]["results"]:
                            if result.document_id not in seen_docs:
                                theme_results.append(result)
                                seen_docs.add(result.document_id)
            
            results_by_theme[theme] = theme_results
            all_theme_results.extend(theme_results)
        
        # Final deduplication across all themes
        deduplicated = self._deduplicate_results(all_theme_results)
        
        return deduplicated, results_by_theme
    
    def _rerank_theme_concepts(
        self,
        theme: str,
        theme_concepts: list[str],
        results_per_concept: dict[str, dict],
        limit_per_concept: int,
        parse_rank
    ) -> list[Result]:
        """
        Apply per-concept reranking for a single theme.
        
        For each concept in the theme:
        1. Sort chunks by source_rank (ASC) then relevance (DESC)
        2. Take top N chunks
        
        Then combine and deduplicate within the theme.
        """
        reranked_results: list[Result] = []
        
        for concept in theme_concepts:
            if concept not in results_per_concept:
                continue
                
            results = results_per_concept[concept]["results"]
            
            # Flatten chunks with metadata
            chunk_entries = []
            for result in results:
                for chunk in result.chunks:
                    chunk_entries.append({
                        "result": result,
                        "chunk": chunk,
                        "relevance": chunk.relevance or 0.0,
                        "rank": parse_rank(result.source_rank),
                    })
            
            # Sort: source_rank ASC, relevance DESC
            chunk_entries.sort(key=lambda x: (x["rank"], -x["relevance"]))
            
            # Take top N
            top_entries = chunk_entries[:limit_per_concept]
            
            # Rebuild Result objects for this concept
            doc_chunks: dict[str, list] = {}
            doc_meta: dict[str, Result] = {}
            
            for entry in top_entries:
                doc_id = entry["result"].document_id
                if doc_id not in doc_chunks:
                    doc_chunks[doc_id] = []
                    doc_meta[doc_id] = entry["result"]
                doc_chunks[doc_id].append(entry["chunk"])
            
            # Create Result objects
            for doc_id, chunks in doc_chunks.items():
                meta = doc_meta[doc_id]
                new_result = Result(
                    document_id=meta.document_id,
                    headline=meta.headline,
                    timestamp=meta.timestamp,
                    source_key=meta.source_key,
                    source_name=meta.source_name,
                    source_rank=meta.source_rank,
                    url=meta.url,
                    ts=meta.ts,
                    document_scope=meta.document_scope,
                    language=meta.language,
                    chunks=tuple(chunks),
                )
                reranked_results.append(new_result)
        
        # Deduplicate within the theme
        return self._deduplicate_results(reranked_results)

   
    def _calculate_source_rank_distribution(
        self, results: list[Result]
    ) -> tuple[dict[int, int], int, dict[str, dict]]:
        """
        Calculate source rank distribution for unique chunks.
        
        Args:
            results: List of deduplicated Result objects
            
        Returns:
            Tuple of:
            - rank_distribution: {rank: count} e.g. {1: 25, 2: 10, 3: 5}
            - total_unique_chunks: Total number of unique chunks
            - source_breakdown: {source_name: {rank: X, count: Y}}
        """
        rank_distribution: dict[int, int] = {}
        source_breakdown: dict[str, dict] = {}
        total_chunks = 0
        
        for result in results:
            rank = result.source_rank or 0
            source_name = result.source_name
            chunk_count = len(result.chunks)
            
            # Update rank distribution
            rank_distribution[rank] = rank_distribution.get(rank, 0) + chunk_count
            total_chunks += chunk_count
            
            # Update source breakdown
            if source_name not in source_breakdown:
                source_breakdown[source_name] = {
                    "rank": rank,
                    "count": 0
                }
            source_breakdown[source_name]["count"] += chunk_count
        
        return rank_distribution, total_chunks, source_breakdown


@log_args
def build_query(
    entity_id: str,
    similarity_text: str | None,
    report_dates: ReportDates,
    *,
    source_filter: list[str] | None,
    categories: list[str] | None = None,
    sentiment_threshold: float | None,
    chunk_limit: int,
    rerank_threshold: float | None,
    source_rank_boost: int | None,
    freshness_boost: int | None,
    headline_search: bool = False,
) -> dict:
    if source_rank_boost is None:
        source_rank_boost = settings.API_SOURCE_RANK_BOOST
    if freshness_boost is None:
        freshness_boost = settings.API_FRESHNESS_BOOST
    query: SearchAPIQueryDict = {
        "auto_enrich_filters": False,  # Our queries are tuned, avoid extra unexpected filters
        "filters": {
            "timestamp": {
                "start": report_dates.start.isoformat(),
                "end": report_dates.end.isoformat(),
            }
        },
        "ranking_params": {
            "source_boost": source_rank_boost,
            "freshness_boost": freshness_boost,
        },
        "max_chunks": chunk_limit,
    }

    if similarity_text:
        query["text"] = similarity_text

    # Check if entity_id is presumably a known entity or a topic
    if len(entity_id) == 6:
        entity_filter = {"any_of": [entity_id]}
        if headline_search:
            entity_filter["search_in"] = "HEADLINE"
        query["filters"]["entity"] = entity_filter
    else:
        raise ValueError(f"Invalid entity ID format: {entity_id}")

    # If a sentiment threshold is provided, filter for strong positive/negative
    # We want to avoid specifically chunks with sentiment 0 as those are often not relevant
    if sentiment_threshold:
        sentiment_threshold = abs(
            sentiment_threshold
        )  # Ensure positive, we only care about magnitude
        query["filters"]["sentiment"] = {
            "ranges": [
                {"min": -1, "max": -sentiment_threshold},
                {"min": sentiment_threshold, "max": 1},
            ]
        }

    if rerank_threshold is None:
        query["ranking_params"]["reranker"] = {"enabled": False}
    else:
        query["ranking_params"]["reranker"] = {
            "enabled": True,
            "threshold": rerank_threshold,
        }

    # Use high-quality sources if desired
    if source_filter:
        query["filters"]["source"] = {"mode": "INCLUDE", "values": source_filter}

    # Filter to a specific source category
    if categories:
        query["filters"]["category"] = {
            "mode": "INCLUDE",
            "values": categories,
        }

    return {"query": query}

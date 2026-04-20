"""
Chunk Filter Service for filtering already-used chunks across runs.

This service tracks which chunk texts have been used in previous runs
by storing their SHA256 hashes. On subsequent runs, chunks with matching
hashes are filtered out to avoid processing duplicate content.
"""
import hashlib
from collections import defaultdict
from datetime import datetime

from bigdata_briefs import logger
from bigdata_briefs.models import Result
from bigdata_briefs.novelty.models import ChunkTextHash
from bigdata_briefs.novelty.storage import ChunkHashStorage


class ChunkFilterService:
    """
    Service for filtering chunks based on text hash to avoid reprocessing
    content that was already used in previous runs.
    """
    
    def __init__(self, chunk_hash_storage: ChunkHashStorage):
        self.storage = chunk_hash_storage

    @staticmethod
    def hash_text(text: str) -> str:
        """
        Compute SHA256 hash of normalized text.
        
        Args:
            text: The chunk text to hash.
            
        Returns:
            64-character hexadecimal SHA256 hash.
        """
        normalized = text.strip()
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    def filter_and_store_chunks(
        self,
        results: list[Result],
        results_by_theme: dict[str, list[Result]],
        entity_id: str,
        start_date: datetime,
        end_date: datetime,
        current_date: datetime,
    ) -> tuple[list[Result], dict[str, list[Result]], int, int, list[dict]]:
        """
        Filter chunks that were already used in previous runs and store new ones.
        
        This function:
        1. Hashes all chunk texts from the results
        2. Retrieves existing hashes from the database for this entity
        3. Identifies chunks to remove (those with hashes already in DB)
        4. Filters both results and results_by_theme
        5. Stores new hashes for future runs
        
        Args:
            results: List of Result objects (already deduplicated by doc_id+chunk_num)
            results_by_theme: Dict mapping theme names to their results
            entity_id: Entity identifier
            start_date: Start of lookback period
            end_date: End of lookback period
            current_date: Current report date (for storing new hashes)
            
        Returns:
            Tuple of:
            - Filtered results
            - Filtered results_by_theme
            - Number of chunks removed
            - Number of new chunks stored
            - List of removed chunk details (for debugging)
        """
        # Step 1: Build mapping of text_hash -> set of chunk_keys
        # Also store chunk metadata for debugging
        hash_to_keys: dict[str, set[str]] = defaultdict(set)
        key_to_hash: dict[str, str] = {}  # For reverse lookup
        key_to_metadata: dict[str, dict] = {}  # For removed chunks details
        
        for result in results:
            for chunk in result.chunks:
                chunk_key = f"{result.document_id}-{chunk.chunk}"
                text_hash = self.hash_text(chunk.text)
                hash_to_keys[text_hash].add(chunk_key)
                key_to_hash[chunk_key] = text_hash
                key_to_metadata[chunk_key] = {
                    "doc_id": result.document_id,
                    "chunk_num": chunk.chunk,
                    "headline": result.headline,
                    "source": result.source_name or result.source_key,
                    "text_preview": chunk.text[:150] + "..." if len(chunk.text) > 150 else chunk.text,
                }
        
        total_chunks = len(key_to_hash)
        
        if total_chunks == 0:
            return results, results_by_theme, 0, 0, []
        
        # Step 2: Retrieve existing hashes from DB
        existing_hashes = self.storage.retrieve_hashes(
            entity_id, start_date, end_date
        )
        
        # Step 3: Find chunk_keys to remove (those with hash already in DB)
        keys_to_remove: set[str] = set()
        for text_hash, chunk_keys in hash_to_keys.items():
            if text_hash in existing_hashes:
                keys_to_remove.update(chunk_keys)
        
        # Step 4: Filter results
        filtered_results = self._filter_results(results, keys_to_remove)
        filtered_by_theme = self._filter_results_by_theme(results_by_theme, keys_to_remove)
        
        # Step 5: Store new hashes (those not already in DB)
        # Store ALL chunk_keys, even if they share the same hash (for complete traceability)
        new_hashes_to_store: list[ChunkTextHash] = []
        
        for chunk_key, text_hash in key_to_hash.items():
            # Only store if: not in existing DB AND not being removed
            if text_hash not in existing_hashes and chunk_key not in keys_to_remove:
                new_hashes_to_store.append(ChunkTextHash(
                    entity_id=entity_id,
                    date=current_date,
                    text_hash=text_hash,
                    chunk_key=chunk_key,
                ))
        
        if new_hashes_to_store:
            self.storage.store_hashes(new_hashes_to_store)
        
        removed_count = len(keys_to_remove)
        stored_count = len(new_hashes_to_store)
        unique_texts = len(hash_to_keys)
        
        # Build list of removed chunk details for debugging
        removed_chunks_details = [
            key_to_metadata[key] for key in sorted(keys_to_remove)
        ]
        
        logger.info(
            f"Chunk hash filter: {total_chunks} total chunks, "
            f"{unique_texts} unique texts, {removed_count} removed (already used), "
            f"{stored_count} new hashes stored"
        )
        
        return filtered_results, filtered_by_theme, removed_count, stored_count, removed_chunks_details

    def _filter_results(
        self,
        results: list[Result],
        keys_to_remove: set[str],
    ) -> list[Result]:
        """
        Filter chunks from results based on keys to remove.
        
        Creates new Result objects with filtered chunks.
        Results with no remaining chunks are excluded entirely.
        """
        if not keys_to_remove:
            return results
            
        filtered: list[Result] = []
        
        for result in results:
            # Filter chunks for this result
            new_chunks = []
            for chunk in result.chunks:
                chunk_key = f"{result.document_id}-{chunk.chunk}"
                if chunk_key not in keys_to_remove:
                    new_chunks.append(chunk)
            
            # Only include result if it has remaining chunks
            if new_chunks:
                filtered.append(Result(
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
                ))
        
        return filtered

    def _filter_results_by_theme(
        self,
        results_by_theme: dict[str, list[Result]],
        keys_to_remove: set[str],
    ) -> dict[str, list[Result]]:
        """
        Filter chunks from results_by_theme based on keys to remove.
        """
        if not keys_to_remove:
            return results_by_theme
            
        filtered_by_theme: dict[str, list[Result]] = {}
        
        for theme, theme_results in results_by_theme.items():
            filtered_results = self._filter_results(theme_results, keys_to_remove)
            if filtered_results:  # Only include theme if it has results
                filtered_by_theme[theme] = filtered_results
        
        return filtered_by_theme


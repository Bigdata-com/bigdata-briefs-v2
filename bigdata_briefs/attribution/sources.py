from collections import defaultdict

from bigdata_briefs import logger
from bigdata_briefs.attribution.models import RetrievedSourcesReverseMap
from bigdata_briefs.models import (
    AnalysisResponse,
    MergedChunkForPrompt,
    Result,
    RetrievedSources,
    SingleEntityReport,
    SourceChunkReference,
    TopicCollection,
    TopicCollectionNoScore,
    TopicMetadata,
    TopicMetadataNoScore,
)


def create_sources_for_results(
    results: list[Result],
) -> tuple[RetrievedSources, RetrievedSourcesReverseMap]:
    """
    Create a mapping of document IDs to reference IDs and metadata from a list of Results.
    """
    report_sources = {}
    reverse_map = {}
    ref_counter = 1

    for result in results:
        if not result.document_id:
            continue

        for chunk in result.chunks:
            key = f"{result.document_id}-{chunk.chunk}"
            
            # Skip if already processed (deduplication)
            if key in report_sources:
                continue
            
            doc_reference = SourceChunkReference(
                ref_id=ref_counter,
                document_id=result.document_id,
                chunk_id=chunk.chunk,
                headline=result.headline,
                source_key=result.source_key,
                source_name=result.source_name,
                source_rank=result.source_rank,
                ts=result.ts,
                document_scope=result.document_scope,
                language=result.language,
                url=result.url,
                text=chunk.text,
                highlights=chunk.highlights,
            )

            report_sources[key] = doc_reference
            reverse_map[ref_counter] = key
            ref_counter += 1

    return RetrievedSources(root=report_sources), RetrievedSourcesReverseMap(
        root=reverse_map
    )


def merge_chunks_by_text(
    results: list[Result],
    report_sources: RetrievedSources,
) -> tuple[list[MergedChunkForPrompt], dict[int, list[str]]]:
    """
    Merge chunks with identical text into single entries for prompt generation.
    
    When multiple news sources contain the same text (syndicated content, press releases, etc.),
    this function groups them under a single prompt entry with multiple headlines/sources.
    This reduces redundancy and helps the LLM focus on unique content.
    
    Args:
        results: List of Result objects from search
        report_sources: Mapping of "doc_id-chunk_num" to SourceChunkReference
        
    Returns:
        - merged_chunks: List of MergedChunkForPrompt (single or merged chunks)
        - merged_ref_expansion: Mapping of merged_ref_id → list of original keys
                               Used to expand citations after LLM generation
    """
    # Group chunks by their text content
    text_to_chunks: dict[str, list[dict]] = defaultdict(list)
    
    for result in results:
        for chunk in result.chunks:
            key = f"{result.document_id}-{chunk.chunk}"
            ref = report_sources.root.get(key)
            if ref:
                text_normalized = chunk.text.strip()
                text_to_chunks[text_normalized].append({
                    "key": key,
                    "ref_id": ref.ref_id,
                    "headline": result.headline,
                    "source": result.source_name,
                    "text": text_normalized,
                })
    
    # Create MergedChunkForPrompt for each unique text
    merged_chunks: list[MergedChunkForPrompt] = []
    merged_ref_expansion: dict[int, list[str]] = {}
    merged_ref_counter = 1
    
    for text, occurrences in text_to_chunks.items():
        merged_chunk = MergedChunkForPrompt(
            text=text,
            merged_ref_id=merged_ref_counter,
            original_ref_ids=[occ["ref_id"] for occ in occurrences],
            headlines=[occ["headline"] for occ in occurrences],
            sources=[occ["source"] for occ in occurrences],
            original_keys=[occ["key"] for occ in occurrences],
        )
        merged_chunks.append(merged_chunk)
        merged_ref_expansion[merged_ref_counter] = [occ["key"] for occ in occurrences]
        merged_ref_counter += 1
    
    # Log merge statistics
    total_original = sum(len(occs) for occs in text_to_chunks.values())
    merged_count = sum(1 for c in merged_chunks if c.is_merged)
    
    if merged_count > 0:
        logger.info(
            f"Same-text merge: {total_original} chunks → {len(merged_chunks)} unique "
            f"({merged_count} merged entries with multiple sources)"
        )
    
    return merged_chunks, merged_ref_expansion


def replace_references_in_topic_metadata(
    input_metadata: TopicMetadata,
    reverse_map: RetrievedSourcesReverseMap,
    entity,
    merged_ref_expansion: dict[int, list[str]] | None = None,
) -> TopicMetadata:
    """
    Replace reference IDs in the source_attribution of a TopicMetadata object
    with their original document IDs and chunk numbers.
    
    When merged_ref_expansion is provided, merged reference IDs are expanded
    to their original document keys. For example, if the LLM cited merged_ref "1"
    which represented chunks from doc1-1 and doc2-3, this expands to both keys.
    
    Args:
        input_metadata: The TopicMetadata with LLM-generated reference IDs
        reverse_map: Mapping of ref_id → "doc_id-chunk_num" (for non-merged mode)
        entity: Entity object for logging
        merged_ref_expansion: Optional mapping of merged_ref_id → list of original keys
    """
    updated_source_attribution = []

    for ref_id in input_metadata.source_citation:
        try:
            ref_int = int(ref_id)
            
            # Check if this is a merged reference that needs expansion
            if merged_ref_expansion and ref_int in merged_ref_expansion:
                # Merged ref → multiple original keys
                original_keys = merged_ref_expansion[ref_int]
                updated_source_attribution.extend(original_keys)
            elif source_id := reverse_map.get(ref_int):
                # Normal ref → single key
                updated_source_attribution.append(source_id)
            else:
                logger.warning(
                    f"Reference ID {ref_id} not found in reverse map or merged_ref_expansion for {entity}."
                )
                updated_source_attribution.append("")
        except (ValueError, TypeError):
            logger.warning(f"Invalid reference ID format: {ref_id} for {entity}.")
            updated_source_attribution.append("")

    return TopicMetadata(
        topic=input_metadata.topic,
        relevance_score=input_metadata.relevance_score,
        source_citation=updated_source_attribution,
    )


def replace_references_in_topic_collection(
    input_collection: TopicCollection,
    reverse_map: RetrievedSourcesReverseMap,
    entity,
    merged_ref_expansion: dict[int, list[str]] | None = None,
) -> TopicCollection:
    """
    Replace reference IDs in the source_attribution of all TopicMetadata objects
    in a TopicCollection.

    Args:
        input_collection (TopicCollection): The TopicCollection object to process.
        reverse_map (RetrievedSourcesReverseMap): A Pydantic model containing a nested mapping of reference IDs
                                to document IDs and chunk mappings.
        merged_ref_expansion: Optional mapping of merged_ref_id → list of original keys
                             for expanding merged references.

    Returns:
        TopicCollection: A new TopicCollection object with updated source_attribution.
    """
    updated_topics = [
        replace_references_in_topic_metadata(topic, reverse_map, entity, merged_ref_expansion)
        for topic in input_collection.collection
    ]
    return TopicCollection(collection=updated_topics)


def replace_references_in_topic_metadata_no_score(
    input_metadata: TopicMetadataNoScore,
    reverse_map: RetrievedSourcesReverseMap,
    entity,
    merged_ref_expansion: dict[int, list[str]] | None = None,
) -> TopicMetadataNoScore:
    """
    Replace reference IDs in source_citation of a TopicMetadataNoScore with original document keys.
    Same logic as replace_references_in_topic_metadata but for the no-score model.
    """
    updated_source_attribution = []

    for ref_id in input_metadata.source_citation:
        try:
            ref_int = int(ref_id)

            if merged_ref_expansion and ref_int in merged_ref_expansion:
                original_keys = merged_ref_expansion[ref_int]
                updated_source_attribution.extend(original_keys)
            elif source_id := reverse_map.get(ref_int):
                updated_source_attribution.append(source_id)
            else:
                logger.warning(
                    f"Reference ID {ref_id} not found in reverse map or merged_ref_expansion for {entity}."
                )
                updated_source_attribution.append("")
        except (ValueError, TypeError):
            logger.warning(f"Invalid reference ID format: {ref_id} for {entity}.")
            updated_source_attribution.append("")

    return TopicMetadataNoScore(
        topic=input_metadata.topic,
        source_citation=updated_source_attribution,
    )


def replace_references_in_topic_collection_no_score(
    input_collection: TopicCollectionNoScore,
    reverse_map: RetrievedSourcesReverseMap,
    entity,
    merged_ref_expansion: dict[int, list[str]] | None = None,
) -> TopicCollectionNoScore:
    """
    Replace reference IDs in all TopicMetadataNoScore items in the collection.
    """
    updated_topics = [
        replace_references_in_topic_metadata_no_score(
            topic, reverse_map, entity, merged_ref_expansion
        )
        for topic in input_collection.collection
    ]
    return TopicCollectionNoScore(collection=updated_topics)


def process_topic_collection_no_score(
    topic_collection: TopicCollectionNoScore, report_sources: RetrievedSources
) -> tuple[list[str], list[list[str]]]:
    """
    Process TopicCollectionNoScore to topics and citations only (no relevance scores).
    Returns (topics, citations) where citations are list of CQS:source_id strings per topic.
    """
    topics = []
    citations = []

    for topic_metadata in topic_collection.collection:
        topic_citations = []
        for source_id in topic_metadata.source_citation:
            metadata = report_sources.get(source_id)
            if metadata:
                topic_citations.append(f"CQS:{source_id}")
        topics.append(SingleEntityReport.strip_double_asterisks(topic_metadata.topic))
        citations.append(topic_citations)

    return topics, citations


def process_topic_collection(
    topic_collection: TopicCollection, report_sources: RetrievedSources
) -> tuple[list[str], list[list[str]], list[int]]:
    """
    Processes a TopicCollection object to generate topics, citations (separate), and relevance scores.
    Citations are NOT embedded in topic text - they are returned as a separate list.

    Args:
        topic_collection (TopicCollection): The parsed response.
        report_sources (RetrievedSources): A mapping of document IDs to metadata.

    Returns:
        tuple: (topics, citations, relevance_scores)
            - topics (list[str]): Topic texts WITHOUT citations.
            - citations (list[list[str]]): Citations for each topic (e.g. [["CQS:a-1"], ["CQS:b-2", "CQS:c-3"]]).
            - relevance_scores (list[int]): Relevance scores for each topic.
    """
    topics = []
    citations = []
    relevance_scores = []

    for topic_metadata in topic_collection.collection:
        # Collect ALL citations from the LLM output
        topic_citations = []
        for source_id in topic_metadata.source_citation:
            metadata = report_sources.get(source_id)
            if metadata:
                topic_citations.append(f"CQS:{source_id}")

        # Store text and citations separately (strip ** markdown from LLM output)
        topics.append(SingleEntityReport.strip_double_asterisks(topic_metadata.topic))
        citations.append(topic_citations)
        relevance_scores.append(topic_metadata.relevance_score)

    return topics, citations, relevance_scores


def format_bullet_with_citations(text: str, citations: list[str]) -> str:
    """
    Formats a bullet point by appending citations at the end.
    This should only be called when generating final output.

    Args:
        text: The bullet point text (without citations).
        citations: List of citation IDs (e.g. ["CQS:a-1", "CQS:b-2"]).

    Returns:
        Formatted text with citations appended (e.g. "Text `:ref[LIST:[CQS:a-1][CQS:b-2]]`").
    """
    if not citations:
        return text
    refs_str = "][".join(citations)
    return f"{text} `:ref[LIST:[{refs_str}]]`"


def consolidate_report_sources(
    consolidated_sources: RetrievedSources, new_sources: RetrievedSources
):
    """
    Consolidates a new RetrievedSources into the existing consolidated map.

    Args:
        consolidated_sources (RetrievedSources): The consolidated set of sources for all entities.
        new_sources (RetrievedSources): The new sources to merge.
    """

    for source_id, new_doc_data in new_sources.items():
        if source_id not in consolidated_sources:
            # If the document ID is not in the consolidated map, add it directly
            consolidated_sources.set(source_id, new_doc_data)
        else:
            # If the document ID exists, consolidate the chunks
            existing_doc_data = consolidated_sources.get(source_id)
            # Sync valid status
            if new_doc_data.is_referenced():
                existing_doc_data.mark_as_used()

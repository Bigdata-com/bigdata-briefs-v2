import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from jinja2 import Template
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from bigdata_briefs import logger
from bigdata_briefs.novelty.bullet_pipeline_checkpoint import BulletPipelineCheckpoint
from bigdata_briefs.settings import settings
from bigdata_briefs.templates import loader

MAX_CHUNKS_PER_DOCUMENT = 10
REFERENCE_REGEX = re.compile(r"`:ref\[LIST:\[.*?\]\]`")


class NoInfoReportGenerationStep(StrEnum):
    BEFORE_EXPLORATORY_SEARCH = "BEFORE_EXPLORATORY_SEARCH"
    EXPLORATORY_SEARCH = "EXPLORATORY_SEARCH"
    FOLLOW_UP_QUESTIONS = "FOLLOW_UP_QUESTIONS"
    QA_PAIRS = "QA_PAIRS"
    NOVELTY = "NOVELTY"


class ConceptWorkflowMode(StrEnum):
    """Processing modes for concept-based workflow (when topics=["{entity}"]).
    
    - ITERATIVE_SEQUENTIAL_WITH_THEMATIC_CHUNKS: Only theme-specific chunks per iteration (focused)
    """
    ITERATIVE_SEQUENTIAL_WITH_THEMATIC_CHUNKS = "iterative_sequential_with_thematic_chunks"


class ConsolidationMode(StrEnum):
    """Consolidation strategy for merging similar bullet points.
    
    - LOOSE: Conservative, only merges truly similar bullets that share the same names/numbers/events (default)
    - AGGRESSIVE: Groups by theme and creates comprehensive paragraphs (more merging, may lose detail)
    """
    LOOSE = "loose"
    AGGRESSIVE = "aggressive"


class Entity(BaseModel):
    id: str
    name: str
    entity_type: str
    ticker: Annotated[str | None, Field(default=None, validation_alias="metadata_1")]

    _raw: Any = None  # Field used to keep the original response from SDK

    @classmethod
    def from_api(cls, api_entity):
        raw = api_entity
        instance = cls(
            id=raw["id"],
            name=raw["name"],
            entity_type=raw["category"],
            metadata_1=raw.get("ticker", None),
        )
        instance._raw = raw
        return instance

    def get_raw(self):
        return self._raw

    def to_entity_info(self):
        raw = self.get_raw()
        if raw is None:
            # Fallback when raw data is not available (e.g., pipeline steps)
            return EntityInfo(
                id=self.id,
                name=self.name,
                description=None,
                entity_type=self.entity_type or "unknown",
                company_type=None,
                country=None,
                sector=None,
                industry_group=None,
                industry=None,
                ticker=self.ticker,
                webpage=None,
                isin_values=None,
                cusip_values=None,
                sedol_values=None,
                listing_values=None,
            )
        return EntityInfo(
            id=self.id,
            name=self.name,
            description=raw.get("description", None),
            entity_type=raw["category"],
            company_type=raw.get("type", None),
            country=raw.get("country", None),
            sector=raw.get("sector", None),
            industry_group=raw.get("industry_group", None),
            industry=raw.get("industry", None),
            ticker=raw.get("ticker", None),
            webpage=raw.get("webpage", None),
            isin_values=raw.get("isin_values", None),
            cusip_values=raw.get("cusip_values", None),
            sedol_values=raw.get("sedol_values", None),
            listing_values=raw.get("listing_values", None),
        )


class ChunkHighlight(BaseModel):
    pnum: int = Field(description="Paragraph number")
    snum: int = Field(description="Sentence number")


class Chunk(BaseModel):
    """Represents a snippet of text from a single result document."""

    text: str
    chunk: int
    relevance: float
    sentiment: float
    highlights: list[ChunkHighlight]

    model_config = ConfigDict(frozen=True)

    @classmethod
    def from_api(cls, api_chunk):
        return cls(
            text=api_chunk["text"],
            chunk=api_chunk["cnum"],
            relevance=api_chunk["relevance"],
            sentiment=api_chunk["sentiment"],
            highlights=[
                ChunkHighlight(pnum=sentence["paragraph"], snum=sentence["sentence"])
                for sentence in api_chunk.get("sentences", [])
            ],  # Not being returned for now, will be empty until they are added to the API response
        )

    def __hash__(self) -> int:
        return hash((self.text, self.chunk))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Chunk):
            return (self.text, self.chunk) == (other.text, other.chunk)
        return NotImplemented


class Result(BaseModel):
    """Represents a single search result."""

    document_id: str
    headline: str
    timestamp: str
    source_key: str
    source_name: str
    source_rank: int | None = None
    url: str | None = None
    ts: str
    document_scope: str
    language: str
    chunks: tuple[Chunk, ...]

    model_config = ConfigDict(frozen=True)

    @field_validator("chunks", mode="after")
    @classmethod
    def filter_and_sort(cls, chunks):
        return tuple(sorted(chunks[:MAX_CHUNKS_PER_DOCUMENT], key=lambda x: x.chunk))

    @classmethod
    def from_api(cls, api_document):
        match api_document["source"]["rank"]:
            case "RANK_1":
                rank_int = 1
            case "RANK_2":
                rank_int = 2
            case "RANK_3":
                rank_int = 3
            case "RANK_4":
                rank_int = 4
            case "RANK_5":
                rank_int = 5
            case _:
                raise ValueError(
                    f"Unknown source rank {api_document['source']['rank']} for document {api_document['id']}"
                )
        return cls(
            document_id=api_document["id"],
            headline=api_document["headline"],
            timestamp=api_document["timestamp"],
            source_name=api_document["source"]["name"],
            source_key=api_document["source"]["id"],
            source_rank=rank_int,
            url=api_document.get("url", None),
            ts=api_document["timestamp"],
            document_scope=api_document.get("document_type", "Unknown"),
            language=api_document.get("language", "Unknown"),
            chunks=[Chunk.from_api(api_chunk) for api_chunk in api_document["chunks"]],
        )


class MergedChunkForPrompt(BaseModel):
    """
    A chunk potentially merged from multiple sources with identical text.
    
    Used when DEDUPLICATE_SAME_TEXT is enabled to reduce redundancy in prompts.
    Multiple news articles may contain the same text (syndicated content, press releases, etc.)
    This model groups them under a single prompt entry with multiple headlines/sources.
    """
    
    text: str  # The unique text content
    merged_ref_id: int  # Virtual reference ID for this merged chunk
    original_ref_ids: list[int]  # Original reference IDs to expand after LLM generation
    headlines: list[str]  # List of headlines: ["Title1", "Title2", ...]
    sources: list[str]  # List of source names: ["Reuters", "Bloomberg", ...]
    original_keys: list[str]  # Original keys: ["doc1-chunk1", "doc2-chunk3", ...]
    
    @property
    def is_merged(self) -> bool:
        """Returns True if this chunk was merged from multiple sources."""
        return len(self.original_ref_ids) > 1
    
    @property
    def headline_count(self) -> int:
        """Returns the number of headlines (sources) for this chunk."""
        return len(self.headlines)


class StartEndDate(BaseModel):
    start: datetime
    end: datetime

    def get_lookback_days(self):
        return (self.end - self.start).days

    def is_single_day(self) -> bool:
        """Check if the period covers exactly one calendar day."""
        actual_end = self.end - timedelta(seconds=1)
        return self.start.date() == actual_end.date()

    def get_actual_end_date(self) -> datetime:
        """Get the actual last included day (end at midnight means previous day)."""
        return self.end - timedelta(seconds=1)

    def get_current_date_for_prompt(self) -> str:
        """Get the start date formatted for prompts that embed it in a sentence.

        Always uses the start of the reporting window so that relevance_score
        and novelty_embedding prompts share a consistent reference point.
        For bullets_generation use get_date_phrase_for_prompt() instead.
        """
        return self.start.strftime("%A, %B %d, %Y")

    def get_date_phrase_for_prompt(self) -> str:
        """Return a context-aware date phrase for the bullets_generation header.

        Single day  → 'Today is Monday, April 21, 2026 (temporal reference only — do not use this date as bullet content)'
        Multi-day   → 'We are analyzing the period from ... (temporal reference only ...)'
        """
        suffix = " (temporal reference only — do not include this date in bullet content unless it appears in a source excerpt)"
        if self.is_single_day():
            return f"Today is {self.start.strftime('%A, %B %d, %Y')}{suffix}"
        return (
            f"We are analyzing the period from {self.get_start_date_formatted()}"
            f" to {self.get_end_date_formatted()}{suffix}"
        )

    def get_start_date_formatted(self) -> str:
        """Get start date formatted without time."""
        return self.start.strftime("%B %d, %Y")

    def get_end_date_formatted(self) -> str:
        """Get end date formatted without time (actual last included day)."""
        if self.is_single_day():
            return self.start.strftime("%B %d, %Y")
        return self.get_actual_end_date().strftime("%B %d, %Y")

    def get_date_range_for_log(self) -> str:
        """Get date range string for debug logging."""
        if self.is_single_day():
            return self.start.strftime("%Y-%m-%d")
        return f"{self.start.strftime('%Y-%m-%d')} to {self.get_actual_end_date().strftime('%Y-%m-%d')}"

    def get_date_filter_instructions(self, *, indent: str = "      ") -> str:
        start_s = self.get_start_date_formatted()
        end_s = self.get_end_date_formatted()
        temporal_ref_warning = (
            "IMPORTANT: the date information above is provided ONLY as a temporal reference "
            "to help you determine what is recent and what is not. Do NOT use it as a fact "
            "to include inside a bullet point. The reporting date or fiscal quarter must never "
            "appear as content in a bullet unless it is explicitly stated in one of the source "
            "excerpts in <context>. If a source excerpt contains a date or fiscal period, "
            "you may report it — but only because it comes from the source, not from this "
            "reference date."
        )
        undated = (
            "If a fact has no explicit date, exclude it if it appears to describe "
            "a past event rather than a current development. However, if a past event "
            "has a NEW update or status change (e.g., new progress, new timeline, new outcome), "
            "report ONLY the update, not the original event."
        )
        if self.is_single_day():
            lines = (
                f"Today is {start_s}. Only report developments that occurred or were disclosed "
                f"on or before {start_s}. Future events (scheduled dates, deadlines, upcoming "
                "announcements) may be mentioned, but ONLY as forward-looking references — "
                f"NEVER present factual data (figures, holdings, prices, results) as of a date "
                f"after {start_s}. If a source mentions a figure dated after today, report it "
                "as the most recently known figure rather than attributing it to a future date.",
                temporal_ref_warning,
                "Do NOT generate bullets that only describe past events. Each bullet must be anchored "
                "to a current development. Past facts may be referenced as context within a bullet, "
                "but the bullet itself must report something new or current.",
                f"If a bullet would only state what happened before {start_s} with no connection "
                "to a current development, do not include it.",
                undated,
            )
        else:
            lines = (
                f"The reporting period is {start_s} to {end_s}. Only report developments that "
                "occurred, were disclosed, or are scheduled during this period.",
                temporal_ref_warning,
                "Do NOT generate bullets that only describe past events. Each bullet must be anchored "
                "to a current development within the reporting period. Past facts may be referenced "
                "as context within a bullet, but the bullet itself must report something new or current.",
                f"If a bullet would only state what happened before {start_s} with no connection "
                "to a current development, do not include it.",
                undated,
            )
        return "\n".join(f"{indent}- {line}" for line in lines)


class ReportDates(StartEndDate):
    """Reporting window; LLM novelty always runs when the pipeline reaches novelty check."""

    def get_novelty_dates(self) -> StartEndDate:
        return StartEndDate(
            start=self.start - timedelta(days=settings.NOVELTY_LOOKBACK_DAYS),
            end=self.start,
        )


class FollowUpAnalysis(BaseModel):
    """Generates a list of follow-up questions for further analysis focusing on the most recent news (optional)."""

    questions: list[str] | None = Field(
        description="A list of short, actionable, fully contextualized follow-up questions. (no longer than 12 words).",
    )


class NoveltyCheckResult(BaseModel):
    """Result of LLM-based novelty check for a single bullet point."""

    decision: Literal["KEEP", "DISCARD", "REWRITE"] = Field(
        description="Decision: KEEP (entirely new), DISCARD (all already present), or REWRITE (partial overlap)."
    )
    reason: str = Field(
        description="Explanation of why this decision was made."
    )
    rewritten_text: str | None = Field(
        default=None,
        description="Rewritten bullet point containing only new information. Only provided if decision is REWRITE."
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="IDs of previous bullet points (from the list above) that support this decision. Required for DISCARD and REWRITE; use the exact IDs shown (e.g. A1, B2). Empty list for KEEP.",
    )


class NoveltyClassification(BaseModel):
    """Step 1 response (two-step mode): classification only, no rewritten_text."""

    decision: Literal["KEEP", "DISCARD", "REWRITE"] = Field(
        description="Decision: KEEP (entirely new), DISCARD (all already present), or REWRITE (partial overlap)."
    )
    reason: str = Field(
        description="Explanation of why this decision was made. For REWRITE, describes which facts are known and which are new."
    )
    instruction: str | None = Field(
        default=None,
        description="For REWRITE only: removal instructions for the rewriter (which parts of the new item to remove). Null for KEEP/DISCARD.",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="IDs of previous bullet points that support this decision. Required for DISCARD and REWRITE; empty for KEEP.",
    )


class NoveltyRewrite(BaseModel):
    """Step 2 response (two-step mode): rewritten text with empty-result flag."""

    is_empty: bool = Field(
        default=False,
        description="True when all facts were already known and nothing meaningful remains after rewriting.",
    )
    rewritten_text: str = Field(
        default="",
        description="Rewritten bullet containing only new information. Empty string when is_empty is true.",
    )


class ThematicGroup(BaseModel):
    """Represents a group of bullet points that can be consolidated into one."""

    indices: list[int] = Field(
        description="List of bullet point indices belonging to this group."
    )
    consolidation_rationale: str = Field(
        description="Explanation of why these bullets are similar and should be consolidated."
    )


class ClusteringResult(BaseModel):
    """Result of thematic clustering of bullet points."""

    thematic_groups: list[ThematicGroup] = Field(
        description="List of groups of similar bullet points to consolidate."
    )
    # standalone is calculated automatically as: all_indices - grouped_indices


class ConsolidatedBullet(BaseModel):
    """Result of consolidating multiple bullet points into one."""

    consolidated_text: str = Field(
        description="The merged bullet point text."
    )
    citations: list[str] = Field(
        description="Combined list of source citations from all merged bullets."
    )


class StandaloneAction(StrEnum):
    """Actions for standalone bullet validation (Step 10)."""
    KEEP = "keep"           # Keep as is
    MERGED = "merged"       # Merge into another bullet
    REWRITE = "rewrite"     # Remove redundant parts, keep unique information
    DISCARDED = "discarded" # Remove as duplicate/redundant


class StandaloneBulletItem(BaseModel):
    """Single action item for standalone bullet validation."""
    
    index: str = Field(
        description="Index of the bullet point (e.g., 'C0' for consolidated, 'S2' for standalone)."
    )
    action: StandaloneAction = Field(
        description="Action to take on this bullet point."
    )
    merge_with: list[str] = Field(
        default_factory=list,
        description="For MERGED action: list of bullet indices to merge with (e.g., ['C1', 'S3'])."
    )
    rationale: str = Field(
        description="Explanation of why this action was chosen."
    )


class StandaloneAnalysisPlan(BaseModel):
    """Plan for validating standalone bullet points after theme consolidation (Step 10)."""
    
    actions: list[StandaloneBulletItem] = Field(
        description="List of actions for bullets that need changes. Bullets not listed are kept as-is."
    )


class RewrittenBullet(BaseModel):
    """Result of rewriting a bullet point."""
    
    rewritten_text: str = Field(
        description="The rewritten bullet point text."
    )
    preserved_citations: list[str] = Field(
        default_factory=list,
        description="Citations preserved from the original bullet."
    )


class MergedBullet(BaseModel):
    """Result of merging multiple bullet points into one (for deduplication)."""
    
    merged_text: str = Field(
        description="The merged bullet point text containing all unique information."
    )


class ConceptCategory(BaseModel):
    """Represents a thematic category containing related concepts."""

    theme: str = Field(
        description="Brief theme name describing the category (e.g., 'Financial Performance', 'Strategic Actions')."
    )
    concepts: list[str] = Field(
        description="List of related concepts within this theme. Maximum 3 concepts per category.",
        max_length=3,
    )


class ConceptExtraction(BaseModel):
    """Extracts thematic lists of fundamental concepts related to an entity from text chunks."""

    categories: list[ConceptCategory] = Field(
        description="List of thematic categories with related concepts. Maximum 5 categories.",
        max_length=5,
    )


class SourceChunkReference(BaseModel):
    ref_id: int
    document_id: str
    headline: str
    ts: str
    document_scope: str
    language: str
    source_key: str
    source_name: str
    source_rank: int | None = None
    url: str | None = None
    chunk_id: int
    text: str
    highlights: list[ChunkHighlight]
    _is_referenced: bool = False

    def is_referenced(self):
        return self._is_referenced

    def mark_as_used(self):
        self._is_referenced = True


class ReportedSources(RootModel):
    root: dict[
        str, SourceChunkReference
    ]  # The key is the reference ID and the value includes everything needed to reference the source


class RetrievedSources(ReportedSources):
    root: dict[
        str, SourceChunkReference
    ]  # The key is the reference ID and the value includes everything needed to reference the source

    def keys(self):
        """Return the keys of the underlying dictionary."""
        return self.root.keys()

    def values(self):
        """Return the values of the underlying dictionary."""
        return self.root.values()

    def items(self):
        """Return the items of the underlying dictionary."""
        return self.root.items()

    def get(self, key):
        """Get a value from the underlying dictionary."""
        return self.root.get(key)

    def set(self, key, value):
        """Set a value in the underlying dictionary."""
        self.root[key] = value

    def __contains__(self, key):
        """Check if a key exists in the underlying dictionary."""
        return key in self.root

    def __bool__(self):
        """Return True if the dictionary is not empty."""
        return bool(self.root)

    def __len__(self):
        """Return the number of items in the dictionary."""
        return len(self.root)

    def __getitem__(self, key):
        """Get an item from the underlying dictionary."""
        return self.root[key]

    @model_serializer
    def serialize(self):
        """
        Serialize only used references into a Python dict.
        """
        serialized_data = {
            ref_id: doc.model_dump()
            for ref_id, doc in self.items()
            if doc.is_referenced()
        }

        return serialized_data


class EntityInfo(BaseModel):
    """Model representing entity information for entities in briefs reports"""

    id: str
    name: str
    description: str | None = None
    entity_type: str | None = None
    company_type: str | None = None
    country: str | None = None
    sector: str | None = None
    industry_group: str | None = None
    industry: str | None = None
    gender: str | None = None
    ticker: str | None = None
    webpage: str | None = None
    isin_values: list[str] | None = None
    cusip_values: list[str] | None = None
    sedol_values: list[str] | None = None
    listing_values: list[str] | None = None
    market_cap: str | None = None


class SingleEntityReport(BaseModel):
    """A single entity report."""

    entity_id: str
    entity_info: dict  # EntityInfo has a lot of None values, we don't want to include them in the database that is why we dump with excude_none=True
    report_bulletpoints: Annotated[list[str], Field(exclude=True)] = []
    bullet_citations: Annotated[list[list[str]], Field(exclude=True)] = []  # Citations separate from text
    relevance_score: Annotated[list[int], Field(exclude=True)] = []
    clean_final_report: str
    # Pipeline stage tracking (excluded from serialization, used for API response)
    raw_bulletpoints: Annotated[list[str], Field(exclude=True)] = []
    raw_citations: Annotated[list[list[str]], Field(exclude=True)] = []
    post_novelty_bulletpoints: Annotated[list[str], Field(exclude=True)] = []
    post_novelty_citations: Annotated[list[list[str]], Field(exclude=True)] = []
    post_redundancy_bulletpoints: Annotated[list[str], Field(exclude=True)] = []
    post_redundancy_citations: Annotated[list[list[str]], Field(exclude=True)] = []
    # Debug: bullets discarded by relevance (score <= threshold) in theme loop
    relevance_discarded: Annotated[list[dict], Field(exclude=True)] = []
    # Pipeline checkpoint state (generation → novelty); excluded from default API dump
    bullet_pipeline_checkpoints: list[BulletPipelineCheckpoint] = Field(
        default_factory=list,
        exclude=True,
    )

    _is_no_info_report: bool

    def __init__(self, is_no_info_report: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._is_no_info_report = is_no_info_report

    def is_no_info_report(self):
        if not self.clean_final_report and not self._is_no_info_report:
            logger.error(
                f"Clean final report is empty, but no_info_report is False => {self}"
            )
        return self._is_no_info_report or not self.clean_final_report

    def model_dump_with_bullets(self) -> dict:
        """
        Dump the model including excluded bullet fields.
        
        This is needed for pipeline state serialization where we need
        to preserve bullet points between steps, even though they are
        excluded from the default model_dump() for database storage.
        """
        base_dump = self.model_dump()
        # Include excluded fields explicitly
        base_dump["report_bulletpoints"] = self.report_bulletpoints
        base_dump["bullet_citations"] = self.bullet_citations
        base_dump["relevance_score"] = self.relevance_score
        base_dump["raw_bulletpoints"] = self.raw_bulletpoints
        base_dump["raw_citations"] = self.raw_citations
        base_dump["post_novelty_bulletpoints"] = self.post_novelty_bulletpoints
        base_dump["post_novelty_citations"] = self.post_novelty_citations
        base_dump["post_redundancy_bulletpoints"] = self.post_redundancy_bulletpoints
        base_dump["post_redundancy_citations"] = self.post_redundancy_citations
        base_dump["relevance_discarded"] = self.relevance_discarded
        base_dump["bullet_pipeline_checkpoints"] = [
            c.model_dump(mode="json") for c in self.bullet_pipeline_checkpoints
        ]
        return base_dump

    @staticmethod
    def strip_double_asterisks(text: str) -> str:
        """Remove markdown bold markers (**) from text. Use after parsing LLM-generated bullets."""
        return text.replace("**", "") if text else text

    @staticmethod
    def remove_references(text: str) -> str:
        """
        Cleans the input text by removing inline source attributions in the format `:ref[LIST[...]]`.

        >>> SingleEntityReport.remove_references("This is a test `:ref[LIST[1]]`")
        'This is a test '

        >>> SingleEntityReport.remove_references("This is a second test")
        'This is a second test'
        """
        return REFERENCE_REGEX.sub("", text)

    @staticmethod
    def _extract_references(text: str) -> tuple[str, list[str]]:
        """
        Extracts inline source attributions in the format `:ref[LIST[...]]` from text.

        Returns a tuple of (cleaned_text, list_of_references).

        >>> SingleEntityReport.extract_references("This is a test `:ref[LIST[1]]`")
        ('This is a test ', [':ref[LIST[1]]'])

        >>> SingleEntityReport.extract_references("Text `:ref[LIST[1]]` and `:ref[LIST[2]]` more text")
        ('Text  and  more text', [':ref[LIST[1]]', ':ref[LIST[2]]'])

        >>> SingleEntityReport.extract_references("This is a second test")
        ('This is a second test', [])
        """
        references = REFERENCE_REGEX.findall(text)
        cleaned_text = REFERENCE_REGEX.sub("", text)

        REFERENCE_ID_REGEX = re.compile(r"\[CQS:([A-Z0-9\-]+)\]")
        # Extract IDs into single list
        extracted_ids = []
        for ref in references:
            match = REFERENCE_ID_REGEX.findall(ref)
            extracted_ids.extend(match)
        return cleaned_text.strip(), extracted_ids

    def extract_bulletpoints_and_references(self) -> list[tuple[str, list[str]]]:
        texts = (
            self.clean_final_report.removeprefix("* ").removesuffix(" \n").split("\n*")
        )
        return [SingleEntityReport._extract_references(t) for t in texts]

    def render(self) -> str:
        entity_str = self.entity_info["name"]
        if "ticker" in self.entity_info and self.entity_info["ticker"]:
            entity_str += f" ({self.entity_info['ticker']})"

        return f"#### {entity_str}\n\n{self.remove_references(self.clean_final_report).replace('$', '&#36;')}"


class TopicMetadata(BaseModel):
    """Represents a topic with its relevance score and source attributions."""

    topic: str = Field(
        description="A topic summarized in concise market intelligence update"
    )
    relevance_score: int = Field(
        description="A relevance score, on a scale of 1 (low) to 5 (high) based on: actionability, materiality, and market impact, one score for each topic"
    )
    source_citation: list[int | str] = Field(
        description="A list of integers where each integer is the Reference ID."
    )


class TopicCollection(BaseModel):
    """Generates a list of topics summarized in concise market intelligence update, each topic comes with the associated relevance score and source attribution reference."""

    collection: list[TopicMetadata] = Field(
        description="A list of topics with their relevance scores and source attributions."
    )


class TopicMetadataNoScore(BaseModel):
    """Topic with source citations only; relevance is scored separately via relevance_check."""

    topic: str = Field(
        description="A topic summarized in concise market intelligence update"
    )
    source_citation: list[int | str] = Field(
        description="A list of integers where each integer is the Reference ID."
    )


class TopicCollectionNoScore(BaseModel):
    """Collection of topics without relevance scores; used when relevance is computed in a separate step."""

    collection: list[TopicMetadataNoScore] = Field(
        description="A list of topics with their source attributions."
    )


class RelevanceCheckResult(BaseModel):
    """Result of the relevance_check LLM call for a single bullet."""

    relevance_score: int = Field(
        description="Relevance score from 1 (low) to 5 (high)."
    )
    reason: str = Field(
        description="Brief justification for the score."
    )


class AnalysisResponse(BaseModel):
    """Generates a list of topics summarized in concise market intelligence update, a list of the novelty contexts and a list of the novelty scores of each topic."""

    topics: list[str] = Field(
        description="A list of topics summarized in concise market intelligence update",
    )
    relevance_score: list[int] = Field(
        description="A list of relevance scores, on a scale of 1 (low) to 5 (high) based on: actionability, materiality, and market impact, one score for each topic",
    )

    @model_validator(mode="after")
    def fix_topics_and_relevance_score_length(self):
        if len(self.relevance_score) != len(self.topics):
            self.relevance_score = [
                settings.INTRO_SECTION_MIN_RELEVANCE_SCORE + 1
            ] * len(self.topics)

        return self


class PromptConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    system_prompt: str
    user_template: Template
    llm_kwargs: dict


class BulletPointsUsage(BaseModel):
    bullet_points_before_novelty: int = 0
    bullet_points_after_novelty: int = 0
    bullet_points_stored: int = 0

    def __add__(self, other):
        if not isinstance(other, type(self)):
            raise ValueError(
                f"Can't add items that are not BulletPointsUsage: {type(other)}"
            )

        return BulletPointsUsage(
            bullet_points_before_novelty=self.bullet_points_before_novelty
            + other.bullet_points_before_novelty,
            bullet_points_after_novelty=self.bullet_points_after_novelty
            + other.bullet_points_after_novelty,
            bullet_points_stored=self.bullet_points_stored + other.bullet_points_stored,
        )


class LLMUsage(BaseModel):
    model: str = "N/A"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    n_calls: int = 1
    cost_usd: float = 0.0  # Cost in USD

    def __add__(self, other):
        if not isinstance(other, type(self)):
            raise ValueError(f"Can't add items that are not LLMUsage: {type(other)}")

        if self.model != other.model:
            raise ValueError(
                f"Can't add items that don't share a model: {self.model=} != {other.model}"
            )

        return LLMUsage(
            model=self.model,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            n_calls=self.n_calls + other.n_calls,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def is_empty(self) -> bool:
        """Check if this usage instance has any tokens recorded."""
        return self.total_tokens == 0


class EmbeddingsUsage(BaseModel):
    model: str = "N/A"
    tokens: int = 0
    cost_usd: float = 0.0  # Cost in USD

    def __add__(self, other):
        if not isinstance(other, type(self)):
            raise ValueError(
                f"Can't add items that are not EmbeddingsUsage: {type(other)}"
            )

        if self.model != other.model:
            raise ValueError(
                f"Can't add items that don't share a model: {self.model=} != {other.model}"
            )

        return EmbeddingsUsage(
            model=self.model,
            tokens=self.tokens + other.tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class StepUsage(BaseModel):
    """Tracks LLM usage, embedding usage, timing, and operational metrics for a single pipeline step."""
    
    step_name: str
    # Cost and token metrics
    llm_cost_usd: float = 0.0
    llm_tokens: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_calls: int = 0
    embedding_cost_usd: float = 0.0
    embedding_tokens: int = 0
    duration_seconds: float = 0.0
    
    # BigData API metrics
    api_calls: int = 0
    api_query_units: float = 0.0

    # Operational metrics
    chunks_retrieved: int = 0
    bullets_generated: int = 0
    bullets_discarded: int = 0
    bullets_kept: int = 0
    bullets_merged: int = 0
    bullets_rewritten: int = 0
    concepts_count: int = 0
    themes_count: int = 0
    
    def to_summary_dict(self) -> dict:
        """Convert to a summary dictionary for JSON output."""
        result = {
            "llm_cost_usd": round(self.llm_cost_usd, 6),
            "llm_tokens": self.llm_tokens,
            "llm_prompt_tokens": self.llm_prompt_tokens,
            "llm_completion_tokens": self.llm_completion_tokens,
            "llm_calls": self.llm_calls,
            "embedding_cost_usd": round(self.embedding_cost_usd, 6),
            "embedding_tokens": self.embedding_tokens,
            "duration_seconds": round(self.duration_seconds, 3),
            "total_cost_usd": round(self.llm_cost_usd + self.embedding_cost_usd, 6),
        }
        # Only include non-zero operational metrics
        ops = {}
        if self.api_calls: ops["api_calls"] = self.api_calls
        if self.api_query_units: ops["api_query_units"] = round(self.api_query_units, 4)
        if self.chunks_retrieved: ops["chunks_retrieved"] = self.chunks_retrieved
        if self.bullets_generated: ops["bullets_generated"] = self.bullets_generated
        if self.bullets_discarded: ops["bullets_discarded"] = self.bullets_discarded
        if self.bullets_kept: ops["bullets_kept"] = self.bullets_kept
        if self.bullets_merged: ops["bullets_merged"] = self.bullets_merged
        if self.bullets_rewritten: ops["bullets_rewritten"] = self.bullets_rewritten
        if self.concepts_count: ops["concepts_count"] = self.concepts_count
        if self.themes_count: ops["themes_count"] = self.themes_count
        if ops:
            result["operational"] = ops
        return result


class RetrievalTracker(BaseModel):
    retrieval_timestamp: datetime
    entity_id: str | None
    result: list[Result]

    @field_serializer("retrieval_timestamp")
    def serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat()

    @field_validator("retrieval_timestamp", mode="before")
    @classmethod
    def deserialize_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        elif isinstance(value, str):
            return datetime.fromisoformat(value)
        else:
            raise ValueError(
                "The timestamp field must be a datetime object formatted following ISO-8601."
            )


class TopicContentTracker(BaseModel):
    """Tracks the content of the watchlist report."""

    topic: str
    retrieval: list[RetrievalTracker]

    @property
    def total_documents(self) -> float:
        documents = []
        for retrieval in self.retrieval:
            for result in retrieval.result:
                documents.append(result.document_id)
        return len(set(documents))

    @property
    def total_chunks(self) -> float:
        chunks = 0
        for retrieval in self.retrieval:
            for result in retrieval.result:
                chunks += len(result.chunks)
        return chunks

    def __add__(self, other):
        if not isinstance(other, TopicContentTracker):
            raise TypeError("Unsupported type for addition")
        if self.topic != other.topic:
            raise ValueError("Cannot add content from different topis")

        return TopicContentTracker(
            topic=self.topic,
            retrieval=self.retrieval + other.retrieval,
        )

    @classmethod
    def aggregate_per_topic(
        cls, trackers: list["TopicContentTracker"]
    ) -> dict[str, "TopicContentTracker"]:
        """Aggregates the content per topic."""
        aggregated = {}
        for tracker in trackers:
            if tracker.topic not in aggregated:
                aggregated[tracker.topic] = tracker
            else:
                aggregated[tracker.topic] += tracker
        return aggregated

    @classmethod
    def retrieval_from_sdk_result(
        cls, sdk_results: list[Result], entity_id: str | None = None
    ) -> list[RetrievalTracker]:
        if len(sdk_results) == 0:
            return []
        return [
            RetrievalTracker(
                retrieval_timestamp=datetime.now(),
                entity_id=entity_id,
                result=sdk_results,
            )
        ]

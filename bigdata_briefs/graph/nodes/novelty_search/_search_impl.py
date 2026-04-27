"""
Shared implementation for the novelty-via-search pipeline nodes.

Contains all models, prompts, utility functions, and search helpers
shared across the four novelty search nodes:
  - parse_and_plan_search
  - fetch_search_evidence
  - judge_novelty_by_search
  - rewrite_search_bullets

Extracted from run_search_novelty.py (now deprecated).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from bigdata_briefs import logger
from bigdata_briefs.settings import settings


# ── LLM model config ──────────────────────────────────────────────────────────

_NS_MODEL: str = "gpt-5-mini-2025-08-07"
_NS_MAX_TOKENS: int = 2000
_NS_REASONING_EFFORT: str = "low"


# ── Search config ─────────────────────────────────────────────────────────────

_NS_MAX_CHUNKS: int = 20
_NS_RERANKER_THRESHOLD: float = 0.5
_NS_SEARCH_URL: str = "https://api.bigdata.com/v1/search"
_NS_SENTIMENT_RANGES: list[dict] = [
    {"min": -1.0, "max": -0.3},
    {"min": 0.3, "max": 1.0},
]
_NS_CATEGORY_VALUES: list[str] = ["news"]
_NS_HTTP_TIMEOUT: float = settings.NOVELTY_SEARCH_HTTP_TIMEOUT_SECONDS


# ══════════════════════════════════════════════════════════════════════════════
# Local Pydantic models
# ══════════════════════════════════════════════════════════════════════════════


class _NSClaim(BaseModel):
    text: str = Field(description="The factual claim text")


class _NSSentencePart(BaseModel):
    text: str = Field(description="Text segment from the original sentence")
    search_query: str = Field(description="Search query for this segment")
    claim_indices: list[int] = Field(description="Claim indices belonging to this part")


class _NSSearchResult(BaseModel):
    simple_id: str
    original_doc_id: str
    chunk_num: int
    headline: str
    timestamp: str
    source_name: str
    relevance: float
    chunk_text: str
    url: str = ""
    sentiment: float | None = None


class _NSClaimVerdict(BaseModel):
    claim_index: int
    novelty: Literal["novel", "old", "partially_novel", "novel_trivial", "novel_unsupported"]
    evidence_ids: list[str] = Field(default_factory=list)
    reasoning: str


# LLM response models — passed as ``text_format`` to ``call_with_response_format``


class _NSParseAndPlanResponse(BaseModel):
    claims: list[_NSClaim]
    sentence_parts: list[_NSSentencePart]


class _NSSingleClaimVerdictResponse(BaseModel):
    novelty: Literal["novel", "old", "partially_novel", "novel_trivial", "novel_unsupported"]
    evidence_ids: list[str] = Field(default_factory=list)
    reasoning: str


class _NSRewriteResponse(BaseModel):
    rewritten_sentence: str | None
    action: Literal["keep", "rewrite", "discard"]
    reasoning: str


class _NSRewriteResponseMixed(BaseModel):
    rewritten_sentence: str
    reasoning: str


# ══════════════════════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════════════════════

_PARSE_AND_PLAN_PROMPT = """\
You are a business and investment analyst. Given a sentence about an entity, your task is to:

1. Extract the core factual claim(s)—the main statement(s) of the sentence that matter for investors and can be verified against news or filings. Avoid over-granular decomposition; focus on what the sentence is really asserting.
2. Split the sentence into semantically coherent parts, each with its own search query (only when the sentence contains distinct topics that warrant separate searches).

---

CLAIMS EXTRACTION:

- Identify the core statement(s) of the sentence: what it is really saying that matters for investors and can be verified against news or filings.
- Prefer fewer, high-signal claims over a granular breakdown.

Split into multiple claims only when:
- The sentence contains distinct and already independent statements that could stand as separate verifiable assertions.
- Different segments refer to unrelated events, products, or business areas.

Keep to one claim when:
- The sentence is a single assertion or one coherent statement; keep the claim wording close to the original sentence.
- The sentence ties together related facts into one statement; do not produce a second claim that restates the same assertion from another angle or in more elaborate form.
- Splitting would only reflect a different point of view on the same assertion, not distinct statements.

- Omit notation or metadata that only identifies the entity; omit purely interpretive framing unless stated as fact.

---

SENTENCE PARTS AND SEARCH QUERIES:

The purpose of splitting is to enable focused semantic search. A semantic search engine works best when the query is focused on a single topic.

Split the sentence into parts **only** when the sentence describes **distinct, separate events** — i.e. different facts that could stand as separate news items, not just different aspects or perspectives of the same situation. Do **not** split when the sentence links one fact to another (e.g. a prior event, a contrasting fact): that is one situation with context, not two separate events.

Split when:
- The sentence contains **clearly separate events** (e.g. two independent news items: one about topic A, another about topic B).
- Different parts refer to **unrelated** events, products, or business areas that are not tied by a direct link in the sentence.

Keep together when:
- The sentence is a single assertion strictly connected to factual events that have already happened or are about to happen — do not split it.
- The sentence links one fact to another (e.g. one development in light of a prior or contrasting fact) — splitting would only change the perspective, not separate distinct events.
- Events are consequential (one leads to or results from the other).
- Facts are part of the same announcement or transaction.
- Splitting would lose important causal or temporal context.

EXAMPLES:

Example 1 (KEEP TOGETHER - same transaction):
Sentence: "Amazon acquires Whole Foods for $13.7B to expand into grocery retail."
Reasoning: Acquisition, price, and purpose are all part of the same deal.
Result: 1 sentence_part with search_query "Amazon Whole Foods acquisition grocery"

Example 2 (SPLIT - unrelated events):
Sentence: "Tesla reports record Q3 deliveries and announces a new factory in Mexico."
Reasoning: Deliveries and factory announcement are independent news items.
Result: 2 sentence_parts with search_queries "Tesla Q3 deliveries" and "Tesla factory Mexico"

Example 3 (KEEP TOGETHER - causal chain):
Sentence: "Microsoft invests $10B in OpenAI to integrate GPT into Office products."
Reasoning: Investment and integration are causally linked.
Result: 1 sentence_part with search_query "Microsoft OpenAI investment GPT Office"

Example 4 (KEEP TOGETHER - linked facts, include context for recall):
Sentence: "Eurozone regulator tightens capital rules for banks despite earlier pushback from member states."
Reasoning: The sentence links a new development to a prior or contrasting fact. Keeping together; both the claim text and the search query for this part include the linked fact (prior or contrasting) so that the claim stays anchored and the search has better recall.
Result: 1 sentence_part with search_query "Eurozone regulator tightens capital rules banks member states pushback"

For each sentence part:
- Provide the text segment from the original sentence.
- Generate a search query FROM THE ENTITY'S PERSPECTIVE (always include the entity name).
- When the sentence links facts across parts (e.g. one part refers to a development, another to a prior or contrasting fact), **each** part's search query should include the contextual terms that refer to the other linked fact(s) so that search can recall documents covering both.
- In particular: when you split into two or more parts that are linked, Part 1's search query must include key terms from the other part(s), and Part 2's (and any further part's) search query must include key terms from the other part(s), so that **every** search query carries enough context for recall.
- List which claim indices (0-based) belong to this part.

IMPORTANT:
- Every claim must belong to exactly one sentence part.
- Each search query must include the entity name to ensure searches are entity-specific.
- If the sentence has a single coherent topic, use just one sentence part.
- When in doubt, prefer keeping related facts together rather than splitting.

---

Sentence: "{sentence}"
Entity: "{entity}"

Return JSON:
{{
  "claims": [
    {{"text": "..."}},
    ...
  ],
  "sentence_parts": [
    {{
      "text": "<text segment from the original sentence>",
      "search_query": "<focused search query including entity name>",
      "claim_indices": [0, 1]
    }},
    ...
  ]
}}"""


_SINGLE_CLAIM_NOVELTY_PROMPT = """\
You are a careful evaluator of financial intelligence. Your task is to decide whether a single atomic claim adds verifiable new information relative to a body of evidence.

The claim you will evaluate is one of the claims extracted from the following sentence. The sentence is given so you can interpret the claim in context.

ORIGINAL SENTENCE:
{sentence}

The claim below refers to part(s) of this sentence. Evaluate it in that context, not in isolation. The sentence may contain other unrelated facts — focus only on what the claim refers to.

Entity: {entity}
Reference date: {reference_date}

CLAIM TO EVALUATE:
{claim_text}

EVIDENCE (oldest to newest):

{evidence_text}

---

VERDICT DEFINITIONS:

- "novel": The claim describes a concrete fact, event, decision, figure, transaction, or change that is attributable to a specific source, and neither the claim nor its substance appears in the evidence.

- "partially_novel": The topic appears in the evidence, but the claim adds a materially new element. The new element must be concrete and independently verifiable: a specific figure, a named entity or person, a specific date, or a discrete decision or action that advances the story to a new phase. Qualitative descriptors, analytical framing, or characterizations of the same situation — even if not literally present in the evidence — do not qualify as materially new.

- "old": The claim is equivalent in substance to what the evidence already reports, including rephrasings and restatements that carry no new information. This includes:
  - Exact matches, rewordings or paraphrases of the same information
  - Same state or situation presented with a different degree of certainty or framing
  - Figures or facts similar to those in the evidence referring to the same event or situation — mark "old" even if the numbers are not identical

- "novel_trivial": The claim is not literally present in the evidence, but the element that is "new" is not materially informative. Indicators include: scope or coverage statistics (counts of countries, cities, customers, partners) without accompanying material context; small numeric deltas on figures whose direction and magnitude are already reported; evaluative, comparative, or promotional language not grounded in a sourced fact; general statistics of the entity used as scene-setting; qualitative descriptors or analytical characterizations of a situation already covered by the evidence (e.g. labeling a known trend as "fashion-driven" or inferring downstream consequences such as "harming long-term growth").

- "novel_unsupported": The claim is not in the evidence because it appears to be an inference, opinion, comparative judgment, forward-looking projection, or downstream consequence that is not reported by any source and cannot be verified against the evidence. Use this label also when the evidence actively contradicts the claim.

---

DECISION PROCEDURE (follow in order):

1. Is the substance of the claim already present in the evidence (including rephrasings, similar figures for the same event, or the same situation with different framing)? → "old"
2. Is the claim a sourced factual assertion, or does it appear to be an interpretation, inference, or consequence not reported by any source? → if interpretation without a sourced basis: "novel_unsupported"
3. Is the new element material by the definition above? → if not material: "novel_trivial"
4. Does the topic appear in the evidence at all? → if no overlap: "novel"; if topic present but material new element added: "partially_novel"

**Figures/numbers:**
- Figures do not need to match exactly to be "old"; they can be similar (same event, comparable numbers).
- Different figures may refer to a different event (new phase, new round); do not mark "old" solely because numbers differ.
- Do not perform a logical or plausibility check on the numbers.

When the claim's substance does not clearly meet the "novel" or "partially_novel" bar, prefer the more conservative label.

Cite evidence IDs (e.g. D1-C1) that support your determination.

---

Return JSON:
{{
  "novelty": "novel" | "old" | "partially_novel" | "novel_trivial" | "novel_unsupported",
  "evidence_ids": ["D1-C1", "D3-C2"],
  "reasoning": "Explanation citing specific evidence."
}}

---
REMINDER — What you are judging:

Claim to evaluate: {claim_text}
In the context of the original sentence: {sentence}

"""


_REWRITE_PROMPT_MIXED = """\
You are a financial news editor. You are given a sentence and a list of its claims, each labeled either "old" (already known) or "novel" (new information). Claims labeled "novel_trivial" or "novel_unsupported" must be dropped entirely.

Rewrite the sentence using this structure:

    {entity_name}, <clause recalling the old claims>, <pivot marker> <novel claims>.

ENTITY: "{entity_name}"
The sentence must open with this exact name, character-for-character.

PIVOT MARKERS — use exactly one from this list, no alternatives:
- has now <verb> / have now <verb>
- has just <verb>
- has confirmed
- has disclosed
- has reported

RULES:
1. Open with "{entity_name}", exactly as written.
2. State the old claims as a subordinate clause (e.g. "which reported ...", "after lowering ...", "following ..."). Do not omit them.
3. Place the pivot marker between the old-claim clause and the novel material.
4. The novel material must convey the substance of claims labeled "novel" — but write it as a fluent continuation of the sentence, not a copy-paste of the claim text. The subject after the pivot marker is already "{entity_name}", so do not repeat the entity name. Integrate naturally.
5. Drop claims labeled "novel_trivial" or "novel_unsupported" — do not include them anywhere.
6. The result must read as a single, coherent, publishable sentence — not as two clauses mechanically stitched together.

---

EXAMPLES:

Example 1
  Sentence: "{entity_name} faced criticism over its pricing strategy and announced a 15% price reduction across its core product range."
  Claims:
    - [old] faced criticism over its pricing strategy
    - [novel] {entity_name} announced a 15% price reduction across its core product range
  Rewritten: "{entity_name}, which had faced criticism over its pricing strategy, has now cut prices across its core product range by 15%."

Example 2
  Sentence: "{entity_name} lowered its FY26 guidance in March, with the CFO stating that further downward revisions are possible."
  Claims:
    - [old] lowered FY26 guidance in March
    - [novel] the CFO stated that further downward revisions are possible
  Rewritten: "{entity_name}, after lowering its FY26 guidance in March, has now disclosed that further downward revisions remain possible."

Example 3 — tense shift: old claims move to past in the subordinate clause
  Sentence: "{entity_name} suspended operations in a major export market in 2022 and confirmed the permanent closure of its regional offices."
  Claims:
    - [old] suspended operations in that market in 2022
    - [novel] confirmed the permanent closure of its regional offices
  Rewritten: "{entity_name}, which had suspended operations in that market in 2022, has confirmed the permanent closure of its regional offices."

Example 4 — multiple novel claims combined into one fluent clause
  Sentence: "{entity_name} signaled softer margins earlier in the year, reported a Q4 operating margin of 8.2%, and said cost savings would accelerate in the second half."
  Claims:
    - [old] signaled softer margins earlier in the year
    - [novel] Q4 operating margin of 8.2%
    - [novel] cost savings would accelerate in the second half
  Rewritten: "{entity_name}, which had signaled softer margins, has reported a Q4 operating margin of 8.2% and guided for accelerating cost savings in the second half."

Example 5 — novel_trivial claim is dropped
  Sentence: "{entity_name} posted a net loss in Q2, issued a USD 500m bond to refinance near-term debt, and now operates across 52 markets."
  Claims:
    - [old] posted a net loss in Q2
    - [novel] issued a USD 500m bond to refinance near-term debt
    - [novel_trivial] now operates across 52 markets
  Rewritten: "{entity_name}, which posted a net loss in Q2, has now issued a USD 500m bond to refinance near-term debt."

Example 6 — novel_unsupported claim is dropped
  Sentence: "{entity_name} reported weaker demand in Europe and said the trend could accelerate margin pressure, while disclosing a new USD 300m share buyback programme."
  Claims:
    - [old] reported weaker demand in Europe
    - [novel_unsupported] the trend could accelerate margin pressure
    - [novel] disclosed a new USD 300m share buyback programme
  Rewritten: "{entity_name}, which reported weaker demand in Europe, has disclosed a new USD 300m share buyback programme."

---

SENTENCE:

{sentence}

---

CLAIMS:

{claims_and_verdicts}

---

OUTPUT (JSON):

{{
  "rewritten_sentence": "...",
  "reasoning": "Brief explanation."
}}
"""


_REWRITE_PROMPT_MIXED_NOISE = """\
You are a financial news editor. You are given a sentence and a list of its claims. Some claims are labeled "novel" (keep these), others are labeled "novel_trivial" or "novel_unsupported" (drop these entirely).

Your task: produce a clean sentence containing only the information from claims labeled "novel". Do not add, infer, or paraphrase beyond what is necessary for grammatical correctness. Do not use a pivot marker — there is no known context to contrast with.

ENTITY: "{entity_name}"
The sentence must open with this exact name, character-for-character.

RULES:
1. Open with "{entity_name}", exactly as written.
2. Include only the content from claims labeled "novel".
3. Drop claims labeled "novel_trivial" or "novel_unsupported" entirely.
4. Do not add qualifiers, superlatives, editorial framing, or inferred consequences.
5. Paraphrase only where grammatically necessary to combine multiple novel claims.

---

SENTENCE:

{sentence}

---

CLAIMS:

{claims_and_verdicts}

---

OUTPUT (JSON):

{{
  "rewritten_sentence": "...",
  "reasoning": "Brief explanation."
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Utility functions
# ══════════════════════════════════════════════════════════════════════════════


def _ns_timestamp_to_date(ts: str | None) -> str:
    """Return plain date (YYYY-MM-DD) from an ISO-8601 timestamp."""
    if not ts:
        return ""
    return str(ts).split("T")[0].split(" ")[0]


def _ns_format_evidence_grouped_by_date_and_doc(
    results: list[_NSSearchResult],
) -> str:
    """
    Format evidence chunks for prompts: grouped by date then document.

    Headline is shown once per document; chunks listed with [simple_id].
    """
    if not results:
        return ""
    by_date: dict[str, dict[str, list[_NSSearchResult]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in results:
        date = _ns_timestamp_to_date(r.timestamp)
        by_date[date][r.original_doc_id].append(r)
    lines: list[str] = []
    for date in sorted(by_date.keys()):
        for doc_id in sorted(by_date[date].keys()):
            chunks = by_date[date][doc_id]
            headline = chunks[0].headline if chunks else ""
            lines.append(f'{date} — "{headline}"')
            for r in chunks:
                lines.append(f"  [{r.simple_id}] {r.chunk_text}")
            lines.append("")
    return "\n".join(lines).rstrip()


# ══════════════════════════════════════════════════════════════════════════════
# Validation helpers
# ══════════════════════════════════════════════════════════════════════════════


def _ns_validate_parse_and_plan_response(
    response: _NSParseAndPlanResponse,
    entity: str,
) -> None:
    """
    Validate parse_and_plan response.

    Checks: every claim belongs to exactly one sentence part; all indices valid.
    Raises ``ValueError`` on failure (propagates to bullet failure record).
    """
    claims = response.claims
    sentence_parts = response.sentence_parts

    if not claims:
        raise ValueError("parse_and_plan: no claims extracted from sentence")
    if not sentence_parts:
        raise ValueError("parse_and_plan: no sentence parts generated")

    total_claims = len(claims)
    claim_assigned: set[int] = set()

    for part_idx, part in enumerate(sentence_parts):
        indices = part.claim_indices
        if len(indices) != len(set(indices)):
            raise ValueError(
                f"parse_and_plan: sentence_part[{part_idx}] has duplicate claim indices"
            )
        for claim_idx in indices:
            if claim_idx < 0 or claim_idx >= total_claims:
                raise ValueError(
                    f"parse_and_plan: invalid claim index {claim_idx} in "
                    f"sentence_part[{part_idx}] (total claims: {total_claims})"
                )
            if claim_idx in claim_assigned:
                raise ValueError(
                    f"parse_and_plan: claim {claim_idx} assigned to multiple parts"
                )
            claim_assigned.add(claim_idx)

    missing = set(range(total_claims)) - claim_assigned
    if missing:
        raise ValueError(
            f"parse_and_plan: claims {sorted(missing)} not assigned to any part"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Novelty judgment helpers
# ══════════════════════════════════════════════════════════════════════════════


def _ns_compute_overall_verdict(verdicts: list[_NSClaimVerdict]) -> str:
    """Compute bullet-level verdict from per-claim verdicts (5-label conservative aggregator).

    Verdicts: novel | mixed | mixed_weak | discard_not_new | discard_unsupported
    """
    if not verdicts:
        return "old"
    labels = [v.novelty for v in verdicts]

    # Rule 1 — at least one fully novel claim
    if any(l == "novel" for l in labels):
        if all(l == "novel" for l in labels):
            return "novel"
        # Distinguish two sub-cases of "mixed":
        # - mixed      : novel + old/partially_novel → rewrite with old-context clause + pivot marker
        # - mixed_noise: novel + only trivial/unsupported noise → strip noise, keep novel material
        has_old_context = any(l in ("old", "partially_novel") for l in labels)
        if not has_old_context:
            return "mixed_noise"  # rewriter strips noise, publishes novel claims as clean sentence
        return "mixed"

    # Rule 2 — no fully novel, but at least one partially_novel
    if any(l == "partially_novel" for l in labels):
        # NOTE (future improvement): "partially_novel" is currently a single bucket that covers
        # both strong cases (claim adds a concrete new figure, named decision, or specific date)
        # and weak cases (claim adds vague qualitative framing or an analytical inference).
        # To recover more bullets, this branch could ask the judgment LLM for a
        # partially_novel_strength score ("strong" / "weak") and route strong cases to the
        # rewrite LLM instead of discarding them directly.
        return "mixed_weak"  # currently always discarded — see note above

    # Rule 3 — only old / novel_trivial / novel_unsupported
    if any(l == "novel_unsupported" for l in labels):
        return "discard_unsupported"
    return "discard_not_new"


def _ns_find_part_for_claim(
    claim_index: int,
    sentence_parts: list[_NSSentencePart],
) -> int | None:
    for part_index, part in enumerate(sentence_parts):
        if claim_index in part.claim_indices:
            return part_index
    return None


def _ns_get_evidence_for_claim(
    claim_index: int,
    sentence_parts: list[_NSSentencePart],
    results_per_part: list[list[_NSSearchResult]],
    all_results: list[_NSSearchResult],
) -> list[_NSSearchResult]:
    """
    Return evidence chunks relevant for a specific claim.

    Uses the claim's sentence_part's search results, filtered to chunks that
    survived the merge step (present in ``all_results``).
    Falls back to ``all_results`` when part info is unavailable.
    """
    if not sentence_parts or not results_per_part:
        return all_results
    part_index = _ns_find_part_for_claim(claim_index, sentence_parts)
    if part_index is None or part_index >= len(results_per_part):
        return all_results
    all_ids = {r.simple_id for r in all_results}
    return [r for r in results_per_part[part_index] if r.simple_id in all_ids]


# ══════════════════════════════════════════════════════════════════════════════
# Rewrite helpers
# ══════════════════════════════════════════════════════════════════════════════


def _ns_build_rewrite_claims_and_verdicts(
    claims: list[_NSClaim],
    claim_verdicts: list[_NSClaimVerdict],
) -> str:
    """Build the claims+verdicts section for the rewrite prompt.

    The rewriter operates on the judge's decisions only — evidence and per-claim
    reasoning are intentionally not included, since the classification is the
    only signal needed to restructure the sentence.
    """
    lines: list[str] = []
    for i, verdict in enumerate(claim_verdicts):
        idx = verdict.claim_index
        if 0 <= idx < len(claims):
            lines += [
                f"Claim {i + 1}: {claims[idx].text}",
                f"Verdict {i + 1}: {verdict.novelty}",
                "",
            ]
    return "\n".join(lines).rstrip()


# ══════════════════════════════════════════════════════════════════════════════
# Search functions
# ══════════════════════════════════════════════════════════════════════════════


def _ns_reference_date_to_search_end(reference_date: str) -> str:
    """
    Convert reference date to the search end timestamp.

    Date-only input → (date − 1 day) at 23:59:59.
    Datetime input  → use as-is.
    """
    ref = datetime.fromisoformat(reference_date.replace("Z", "+00:00"))
    if ref.hour == 0 and ref.minute == 0 and ref.second == 0 and ref.microsecond == 0:
        end_date = ref.date() - timedelta(days=1)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
        return end_dt.isoformat()
    return reference_date


def _ns_parse_bigdata_response(data: dict) -> list[dict]:
    """Extract flat list of result dicts from Bigdata.com search response body."""
    raw: list[dict] = []
    for doc in data.get("results", []):
        for chunk in doc.get("chunks", []):
            sent = chunk.get("sentiment")
            if sent is not None and isinstance(sent, (int, float)):
                sent = round(float(sent), 4)
            else:
                sent = None
            raw.append(
                {
                    "id": doc["id"],
                    "headline": doc["headline"],
                    "timestamp": doc["timestamp"],
                    "source": doc["source"],
                    "url": doc.get("url", ""),
                    "relevance": round(chunk.get("relevance", 0.0), 2),
                    "cnum": chunk["cnum"],
                    "chunk_text": chunk["text"],
                    "sentiment": sent,
                }
            )
    return raw


def _ns_assign_simple_ids(raw_results: list[dict]) -> list[_NSSearchResult]:
    """
    Sort results oldest→newest and assign simple IDs (D1-C1, D2-C1 …).

    Documents are numbered chronologically; chunks within each doc sequentially.
    """
    sorted_results = sorted(raw_results, key=lambda r: r["timestamp"])
    doc_id_to_num: dict[str, int] = {}
    doc_counter = 0
    results: list[_NSSearchResult] = []
    for r in sorted_results:
        doc_id = r["id"]
        if doc_id not in doc_id_to_num:
            doc_counter += 1
            doc_id_to_num[doc_id] = doc_counter
        doc_num = doc_id_to_num[doc_id]
        simple_id = f"D{doc_num}-C{r['cnum']}"
        results.append(
            _NSSearchResult(
                simple_id=simple_id,
                original_doc_id=doc_id,
                chunk_num=r["cnum"],
                headline=r["headline"],
                timestamp=r["timestamp"],
                source_name=r["source"]["name"],
                relevance=r["relevance"],
                chunk_text=r["chunk_text"],
                url=r.get("url", "") or "",
                sentiment=r.get("sentiment"),
            )
        )
    return results


def _ns_build_search_payload(
    search_query: str,
    entity_id: str,
    slice_end: str,
    slice_start: str | None = None,
) -> dict:
    """Build the JSON payload for one Bigdata.com search request."""
    timestamp_filter: dict[str, str] = {"end": slice_end}
    if slice_start is not None:
        timestamp_filter["start"] = slice_start
    return {
        "query": {
            "text": search_query,
            "auto_enrich_filters": False,
            "filters": {
                "timestamp": timestamp_filter,
                "entity": {"any_of": [entity_id]},
            },
            "ranking_params": {
                "source_boost": 1,
                "freshness_boost": 1,
                "reranker": {
                    "enabled": True,
                    "threshold": _NS_RERANKER_THRESHOLD,
                },
            },
            "max_chunks": _NS_MAX_CHUNKS,
        }
    }


async def _ns_bigdata_search_slice(
    search_query: str,
    entity_id: str,
    slice_end: str,
    slice_start: str | None,
    api_key: str,
    request_hook,
) -> tuple[list[dict], float]:
    """
    Call Bigdata.com search for a single time slice.

    ``request_hook`` (optional async callable) is awaited before every POST
    so the shared 450 QPM budget is respected.
    Returns (raw_result_dicts, query_units).
    """
    payload = _ns_build_search_payload(
        search_query=search_query,
        entity_id=entity_id,
        slice_end=slice_end,
        slice_start=slice_start,
    )
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-API-KEY": api_key,
    }

    if request_hook is not None:
        await request_hook()

    async with httpx.AsyncClient(timeout=_NS_HTTP_TIMEOUT) as client:
        resp = await client.post(_NS_SEARCH_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    query_units = float(data.get("usage", {}).get("api_query_units", 0.0))
    logger.debug(
        "[novelty_search] search slice query=%r end=%s units=%.3f",
        search_query[:60],
        slice_end,
        query_units,
    )
    return _ns_parse_bigdata_response(data), query_units


async def _ns_multi_query_search(
    search_queries: list[str],
    entity_id: str,
    reference_date: str,
    api_key: str,
    request_hook,
) -> tuple[list[list[_NSSearchResult]], list[_NSSearchResult], int, float]:
    """
    Run one end-only search per query in parallel and merge results.

    Uses ``end_only=True`` (single slice, no start date), matching the default
    ``use_lookback_window=False`` state in the original pipeline.

    Returns:
        results_per_query  — per-query results (consistent simple IDs)
        merged_all         — all results merged + deduped, oldest → newest
        duplicates_removed — chunks removed by chunk_text deduplication
        total_query_units  — total API query units consumed
    """
    search_end = _ns_reference_date_to_search_end(reference_date)
    tasks = [
        _ns_bigdata_search_slice(
            search_query=q,
            entity_id=entity_id,
            slice_end=search_end,
            slice_start=None,
            api_key=api_key,
            request_hook=request_hook,
        )
        for q in search_queries
    ]
    slice_results = await asyncio.gather(*tasks)

    total_query_units = 0.0
    raw_per_query: list[list[dict]] = []
    for results, query_units in slice_results:
        raw_per_query.append(results)
        total_query_units += query_units

    # Deduplicate within each query by chunk_text
    deduped_per_query: list[list[dict]] = []
    for query_results in raw_per_query:
        seen_texts: set[str] = set()
        deduped: list[dict] = []
        for r in query_results:
            if r["chunk_text"] not in seen_texts:
                seen_texts.add(r["chunk_text"])
                deduped.append(r)
        deduped_per_query.append(deduped)

    # Merge all queries: deduplicate by chunk_text across queries
    seen_texts_global: set[str] = set()
    merged_raw: list[dict] = []
    for query_results in deduped_per_query:
        for r in query_results:
            if r["chunk_text"] not in seen_texts_global:
                seen_texts_global.add(r["chunk_text"])
                merged_raw.append(r)

    duplicates_removed = sum(len(q) for q in deduped_per_query) - len(merged_raw)

    # Assign simple IDs to merged results (oldest → newest, consistent numbering)
    merged_all = _ns_assign_simple_ids(merged_raw)

    # Reconstruct per-query results with the final consistent simple IDs
    merged_ids = {(r.original_doc_id, r.chunk_num): r for r in merged_all}
    results_per_query: list[list[_NSSearchResult]] = []
    for query_results in deduped_per_query:
        per_q: list[_NSSearchResult] = []
        for r in query_results:
            key = (r["id"], r["cnum"])
            if key in merged_ids:
                per_q.append(merged_ids[key])
        results_per_query.append(per_q)

    logger.info(
        "[novelty_search] search done queries=%d merged=%d dups_removed=%d units=%.3f",
        len(search_queries),
        len(merged_all),
        duplicates_removed,
        total_query_units,
    )
    return results_per_query, merged_all, duplicates_removed, total_query_units

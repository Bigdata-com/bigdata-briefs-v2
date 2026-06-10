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
from bigdata_briefs.utils import asleep_with_backoff


# ── LLM model config ──────────────────────────────────────────────────────────

_NS_MODEL: str = "gpt-5-mini-2025-08-07"
_NS_MAX_TOKENS: int = 2000
_NS_REASONING_EFFORT: str = "low"


# ── Search config ─────────────────────────────────────────────────────────────

_NS_MAX_CHUNKS: int = 15
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


class _NSPivotRelevanceResult(BaseModel):
    relevance_score: int
    reason: str


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


_REWRITE_PROMPT_NOVEL_WITH_CONTEXT = """\
You are a financial news editor. You are given a sentence and a list of its claims, each labeled either "old" (already known) or "novel" (new information). Claims labeled "novel_trivial" or "novel_unsupported" must be dropped entirely.

Rewrite the sentence using this structure:

    {entity_name}, <clause recalling the old claims>, <pivot marker> <novel claims>.

ENTITY: "{entity_name}"

SUBJECT RULE:
Open with "{entity_name}" ONLY when the original sentence uses {entity_name} as its grammatical subject — i.e. the entity itself did, said, reported, or announced something. When the original sentence opens with an external actor (analysts, prediction markets, investors, a market metric, a regulatory body), preserve that external actor as the grammatical subject of the rewrite. The old-context → pivot → novel structure still applies regardless.

EXPECTATION FRAMING RULE:
When the original sentence uses expectation or forecast language — "is expected to", "is projected to", "is forecast to", "analysts estimate", "analysts project", "consensus forecasts", "is anticipated to" — the claim is an analyst estimate or forward projection, NOT a company disclosure. Do NOT convert it to a disclosure verb (has disclosed, has confirmed, has reported). Preserve the expectation framing using: "is now expected to", "is now projected to", "analysts now project", "is now forecast to", "consensus now shows".

PIVOT MARKERS — when the grammatical subject is {entity_name} AND the claim is a company action or disclosure, use exactly one from this list:
- has now <verb> / have now <verb>
- has just <verb>
- has confirmed
- has disclosed
- has reported

When the grammatical subject is NOT {entity_name}, use a pivot that fits the actual subject naturally (e.g. "are now pricing", "now show", "now indicate"). When the claim is an analyst estimate or forecast (even if {entity_name} is the grammatical subject), use an expectation-preserving pivot: "is now expected to", "is now projected to", "analysts now project".

RULES:
1. If the original sentence opens with "{entity_name}" as subject, open the rewrite with "{entity_name}", exactly as written. Otherwise, preserve the natural subject of the sentence.
2. State the old claims as a subordinate clause (e.g. "which reported ...", "after lowering ...", "following ...", "with ... already at ..."). Do not omit them.
3. Place the pivot marker between the old-claim clause and the novel material.
4. The novel material must convey the substance of claims labeled "novel" — write it as a fluent continuation, not a copy-paste. If the subject is already "{entity_name}", do not repeat the entity name after the pivot.
5. Drop claims labeled "novel_trivial" or "novel_unsupported" — do not include them anywhere.
6. The result must read as a single, coherent, publishable sentence — not as two clauses mechanically stitched together.

---

EXAMPLES:

Example 1 — entity as subject, single old + single novel claim
  Sentence: "{entity_name} faced criticism over its pricing strategy and announced a 15% price reduction across its core product range."
  Claims:
    - [old] faced criticism over its pricing strategy
    - [novel] {entity_name} announced a 15% price reduction across its core product range
  Rewritten: "{entity_name}, which had faced criticism over its pricing strategy, has now cut prices across its core product range by 15%."

Example 2 — entity as subject, old guidance + novel CFO statement
  Sentence: "{entity_name} lowered its FY26 guidance in March, with the CFO stating that further downward revisions are possible."
  Claims:
    - [old] lowered FY26 guidance in March
    - [novel] the CFO stated that further downward revisions are possible
  Rewritten: "{entity_name}, after lowering its FY26 guidance in March, has now disclosed that further downward revisions remain possible."

Example 3 — entity as subject, tense shift: old claims move to past in the subordinate clause
  Sentence: "{entity_name} suspended operations in a major export market in 2022 and confirmed the permanent closure of its regional offices."
  Claims:
    - [old] suspended operations in that market in 2022
    - [novel] confirmed the permanent closure of its regional offices
  Rewritten: "{entity_name}, which had suspended operations in that market in 2022, has confirmed the permanent closure of its regional offices."

Example 4 — entity as subject, multiple novel claims combined into one fluent clause
  Sentence: "{entity_name} signaled softer margins earlier in the year, reported a Q4 operating margin of 8.2%, and said cost savings would accelerate in the second half."
  Claims:
    - [old] signaled softer margins earlier in the year
    - [novel] Q4 operating margin of 8.2%
    - [novel] cost savings would accelerate in the second half
  Rewritten: "{entity_name}, which had signaled softer margins, has reported a Q4 operating margin of 8.2% and guided for accelerating cost savings in the second half."

Example 5 — entity as subject, novel_trivial claim is dropped
  Sentence: "{entity_name} posted a net loss in Q2, issued a USD 500m bond to refinance near-term debt, and now operates across 52 markets."
  Claims:
    - [old] posted a net loss in Q2
    - [novel] issued a USD 500m bond to refinance near-term debt
    - [novel_trivial] now operates across 52 markets
  Rewritten: "{entity_name}, which posted a net loss in Q2, has now issued a USD 500m bond to refinance near-term debt."

Example 6 — entity as subject, novel_unsupported claim is dropped
  Sentence: "{entity_name} reported weaker demand in Europe and said the trend could accelerate margin pressure, while disclosing a new USD 300m share buyback programme."
  Claims:
    - [old] reported weaker demand in Europe
    - [novel_unsupported] the trend could accelerate margin pressure
    - [novel] disclosed a new USD 300m share buyback programme
  Rewritten: "{entity_name}, which reported weaker demand in Europe, has disclosed a new USD 300m share buyback programme."

Example 7 — EXTERNAL subject: original sentence opens with market data, not an entity action
  Sentence: "The analyst consensus target price for {entity_name} is $47.23, while prediction markets are pricing a 56% probability that {entity_name} beats its upcoming quarterly earnings."
  Claims:
    - [old] The analyst consensus target price for {entity_name} is $47.23
    - [novel] Prediction markets are pricing a 56% probability that {entity_name} beats its upcoming quarterly earnings
  Rewritten: "With analyst consensus for {entity_name} at $47.23, prediction markets are now pricing a 56% probability of an earnings beat."
  Note: the original opens with "The analyst consensus..." — an external data point — so the rewrite preserves that framing. "are now pricing" fits the actual subject "prediction markets"; do not substitute "{entity_name} has disclosed...".

Example 8 — EXTERNAL subject: original sentence opens with share price action; one old claim + two novel claims
  Sentence: "{entity_name} shares fell 12% after the company reported weaker-than-expected Q3 results, marking the stock's worst single-day drop in two years and pushing it to a 52-week low."
  Claims:
    - [old] {entity_name} shares fell after weaker-than-expected Q3 results
    - [novel] the drop was the stock's worst single-day decline in two years
    - [novel] the decline pushed the stock to a 52-week low
  Rewritten: "{entity_name} shares, which fell after the company reported weaker-than-expected Q3 results, have now marked their worst single-day decline in two years, pushing the stock to a 52-week low."
  Note: the original opens with "{entity_name} shares..." — the market instrument is the subject — so the rewrite preserves that framing. Do not rewrite as "{entity_name} has disclosed that its shares fell...".

Example 9 — EXPECTATION FRAMING: entity is the subject but the claim is an analyst estimate, not a company disclosure; one old + one novel claim
  Sentence: "{entity_name} is expected to report Q1 2026 revenue of approximately $12.3 billion with EPS of $0.02, while Q2 revenue is forecast at $12.7 billion with EPS of $0.15, supported by higher margins."
  Claims:
    - [old] {entity_name} had guided for Q1 2026 revenue of $11.7–$12.7 billion
    - [novel] Analysts now project Q1 EPS of $0.02 and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins
  WRONG rewrite: "{entity_name}, which had guided for Q1 2026 revenue of $11.7–$12.7 billion, has disclosed Q1 EPS of $0.02 and Q2 revenue guidance of $12.7 billion." ✗ — "is expected to" signals an analyst estimate, not a company disclosure
  Rewritten: "{entity_name}, which had guided for Q1 2026 revenue of $11.7–$12.7 billion, is now projected by analysts to deliver Q1 EPS of $0.02 and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins."

Example 10 — EXPECTATION FRAMING: entity as subject, analyst consensus revision with two novel forward-looking claims
  Sentence: "{entity_name} is forecast to post Q1 fiscal 2027 EPS of $8.93, up 43% year-over-year, with revenue expected at $43.1 billion, reflecting continued data-centre demand."
  Claims:
    - [old] {entity_name} had guided for continued data-centre demand and revenue growth
    - [novel] Analyst consensus now forecasts Q1 fiscal 2027 EPS of $8.93, up 43% year-over-year
    - [novel] Analyst consensus projects Q1 fiscal 2027 revenue of $43.1 billion
  Rewritten: "{entity_name}, which had guided for continued data-centre demand, is now forecast by analysts to deliver Q1 fiscal 2027 EPS of $8.93, up 43% year-over-year, on revenue of $43.1 billion."
  Note: "is forecast to" in the original signals analyst projections on future results. Do not use "has disclosed" or "has reported" — use "is now forecast to" / "analysts now project".

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


_REWRITE_PROMPT_PARTIAL_UPDATE_WITH_CONTEXT = """\
You are a financial news editor. You are given a sentence and a list of its claims. Each claim is labeled either "old" (already covered in prior evidence) or "partially_novel" (the topic is known but the claim adds a specific new detail — a concrete figure, a named entity, a date, or a measurable attribute — not fully present in prior sources).

Rewrite the sentence using this structure:

    <subject>, <subordinate clause summarising the old claims>, <pivot marker> <the new material from partially_novel claims>.

ENTITY: "{entity_name}"

SUBJECT RULE:
Open with "{entity_name}" ONLY when the original sentence uses {entity_name} as its grammatical subject — i.e. the entity itself did, said, reported, or announced something. When the original sentence opens with an external actor (analysts, prediction markets, investors, a market metric, a regulatory body), preserve that external actor as the grammatical subject of the rewrite. The old-context → pivot → novel structure still applies regardless.

EXPECTATION FRAMING RULE:
When the original sentence uses expectation or forecast language — "is expected to", "is projected to", "is forecast to", "analysts estimate", "analysts project", "consensus forecasts", "is anticipated to" — the claim is an analyst estimate or forward projection, NOT a company disclosure. Do NOT convert it to a disclosure verb (has disclosed, has confirmed, has reported). Preserve the expectation framing using: "is now expected to", "is now projected to", "analysts now project", "is now forecast to", "consensus now shows".

PIVOT MARKERS — when the grammatical subject is {entity_name} AND the claim is a company action or disclosure, use exactly one from this list:
- has now <verb> / have now <verb>
- has just <verb>
- has confirmed
- has disclosed
- has reported

When the grammatical subject is NOT {entity_name}, use a pivot that fits the actual subject naturally (e.g. "are now pricing", "now stands at", "now show"). When the claim is an analyst estimate or forecast (even if {entity_name} is the grammatical subject), use an expectation-preserving pivot: "is now expected to", "is now projected to", "analysts now project".

RULES:
1. If the original sentence opens with "{entity_name}" as subject, open the rewrite with "{entity_name}", exactly as written. Otherwise, preserve the natural subject of the sentence.
2. Summarise ALL claims labeled "old" as a single subordinate clause (e.g. "which guided for ...", "after reporting ...", "with ... already at ..."). Use past tense. Do not omit them.
3. Place the pivot marker between the old-claim clause and the new material.
4. After the pivot marker, include the full substance of ALL claims labeled "partially_novel". These claims add specific new details (figures, names, dates, attributes) not fully present in prior evidence — include all of them fluently. Do not repeat the entity name after the pivot when the subject is {entity_name}.
5. Claims labeled "novel_trivial" or "novel_unsupported" must be dropped entirely.
6. The result must be a single, coherent, publishable sentence.

---

EXAMPLES:

Example 1 — entity as subject, one old claim + one partially_novel claim (analyst EPS figure not in evidence):
  Sentence: "Amazon.com Inc. has guided for first-quarter revenue of $173.5B–$178.5B, implying ~13% year-over-year growth, while analysts expect earnings of $1.61 per share, up ~1.2% year-over-year."
  Claims:
    - [old] Amazon.com Inc. has guided for Q1 revenue of $173.5B–$178.5B, implying ~13% YoY growth
    - [partially_novel] Analysts expect Q1 earnings of $1.61 per share, up approximately 1.2% year-over-year
  Rewritten: "Amazon.com Inc., which has guided for first-quarter revenue of $173.5B–$178.5B implying ~13% year-over-year growth, has now disclosed analyst consensus for earnings of $1.61 per share, up approximately 1.2% year-over-year."

Example 2 — entity as subject, one old topic + one partially_novel specific metric:
  Sentence: "Intel Corp. reported first-quarter 2026 results broadly in line with expectations, with operating income of $1.8 billion beating the $1.5 billion consensus."
  Claims:
    - [old] Intel Corp. reported first-quarter 2026 results broadly in line with expectations
    - [partially_novel] Intel's operating income of $1.8 billion beat the consensus of $1.5 billion
  Rewritten: "Intel Corp., which reported first-quarter 2026 results broadly in line with expectations, has now confirmed operating income of $1.8 billion, above the $1.5 billion consensus."

Example 3 — entity as subject, one old partnership context + one partially_novel deal size:
  Sentence: "Microsoft Corp. has expanded its partnership with OpenAI and committed $3 billion in additional investment over the next two years, on top of the $13 billion already deployed."
  Claims:
    - [old] Microsoft Corp. has an existing partnership with OpenAI with $13 billion already invested
    - [partially_novel] Microsoft committed an additional $3 billion investment over the next two years
  Rewritten: "Microsoft Corp., which had already invested $13 billion in OpenAI, has now committed an additional $3 billion over the next two years."

Example 4 — entity as subject, multiple old claims + one partially_novel claim:
  Sentence: "Pfizer Inc. reported Q1 2026 revenues of $14.9 billion, beating consensus by 4%, and raised its full-year EPS guidance to $3.20–$3.40 from the prior $2.80–$3.00 range."
  Claims:
    - [old] Pfizer reported Q1 2026 revenues of $14.9 billion beating consensus by 4%
    - [old] Pfizer raised its full-year EPS guidance
    - [partially_novel] The new full-year EPS guidance range is $3.20–$3.40, up from $2.80–$3.00
  Rewritten: "Pfizer Inc., which reported Q1 2026 revenues of $14.9 billion beating consensus and raised its full-year EPS guidance, has now disclosed the updated range of $3.20–$3.40, up from the prior $2.80–$3.00."

Example 5 — EXTERNAL subject: original sentence opens with analyst data, not an entity action; one old claim + one partially_novel specific figure
  Sentence: "The analyst consensus target price for Intel Corp. is $47.23, implying meaningful downside from current levels, while prediction markets are pricing a 56% probability that Intel beats its upcoming quarterly earnings."
  Claims:
    - [old] The analyst consensus target price for Intel Corp. is $47.23, implying meaningful downside
    - [partially_novel] Prediction markets are pricing a 56% probability that Intel beats its upcoming quarterly earnings (specific probability not in prior evidence)
  Rewritten: "With analyst consensus for Intel Corp. at $47.23 implying meaningful downside, prediction markets are now pricing a 56% probability of an earnings beat."
  Note: the original opens with "The analyst consensus..." — an external data point — so the rewrite preserves that framing. "are now pricing" is the pivot for the subject "prediction markets"; do not substitute "Intel Corp. has disclosed...".

Example 6 — EXTERNAL subject: original sentence opens with short interest data; one old claim + one partially_novel specific figure not in evidence
  Sentence: "Short interest in Intel Corp. has remained elevated for several months, with the latest data showing 8.7% of the float sold short, the highest level since Q2 2023."
  Claims:
    - [old] Short interest in Intel Corp. has remained elevated for several months
    - [partially_novel] The latest short interest stands at 8.7% of the float, the highest since Q2 2023 (specific percentage not in prior evidence)
  Rewritten: "With short interest in Intel Corp. already elevated for several months, the latest data now shows 8.7% of the float sold short — the highest level since Q2 2023."
  Note: the original opens with "Short interest in..." — a market positioning metric — so the rewrite preserves that framing. Do not substitute "Intel Corp. has disclosed that short interest is 8.7%...".

Example 7 — EXPECTATION FRAMING: entity as subject but the claim is an analyst estimate on future results; one old + one partially_novel specific figure
  Sentence: "{entity_name} is expected to report Q1 2026 revenue of approximately $12.3 billion with EPS of $0.02, and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins."
  Claims:
    - [old] {entity_name} had guided for Q1 2026 revenue of $11.7–$12.7 billion
    - [partially_novel] Analyst consensus now projects Q1 EPS of $0.02 and Q2 revenue of $12.7 billion with EPS of $0.15 and higher margins (specific figures not in prior evidence)
  WRONG rewrite: "{entity_name}, which had guided for Q1 2026 revenue of $11.7–$12.7 billion, has disclosed Q1 EPS of $0.02 and Q2 revenue of $12.7 billion." ✗
  Rewritten: "{entity_name}, which had guided for Q1 2026 revenue of $11.7–$12.7 billion, is now projected by analysts to deliver Q1 EPS of $0.02 and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins."
  Note: "is expected to" in the original signals analyst estimates on future results. Preserve that framing — do not use "has disclosed" or "has confirmed".

Example 8 — EXPECTATION FRAMING: entity as subject, consensus price target revision with one partially_novel new specific figure
  Sentence: "Tesla Inc. is expected to generate FY2026 revenue of $116 billion with consensus EPS of $3.58, reflecting a recovery in delivery volumes."
  Claims:
    - [old] Tesla Inc. had guided for a recovery in delivery volumes in 2026
    - [partially_novel] Consensus now forecasts FY2026 revenue of $116 billion and EPS of $3.58 (specific figures not in prior evidence)
  Rewritten: "Tesla Inc., which had guided for a recovery in delivery volumes in 2026, is now projected by consensus to deliver FY2026 revenue of $116 billion and EPS of $3.58."
  Note: both old and novel claims are analyst projections on future results. Do not use "has disclosed" or "has confirmed".

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


_REWRITE_PROMPT_NOVEL_NOISY = """\
You are a financial news editor. You are given a sentence and a list of its claims. Some claims are labeled "novel" (keep these), others are labeled "novel_trivial" or "novel_unsupported" (drop these entirely).

Your task: produce a clean sentence containing only the information from claims labeled "novel". Do not add, infer, or paraphrase beyond what is necessary for grammatical correctness. Do not use a pivot marker — there is no known context to contrast with.

ENTITY: "{entity_name}"

SUBJECT RULE:
Open with "{entity_name}" ONLY when the original sentence uses {entity_name} as its grammatical subject. When the original sentence opens with an external actor (analysts, prediction markets, investors, a market metric), preserve that external actor as the grammatical subject of the rewrite.

EXPECTATION FRAMING RULE:
When the original sentence uses expectation or forecast language — "is expected to", "is projected to", "is forecast to", "analysts estimate", "analysts project" — the claim is an analyst estimate on future results, NOT a company disclosure. Preserve that language in the rewrite. Do NOT rephrase as "has disclosed", "has confirmed", or "has reported".

RULES:
1. If the original sentence opens with "{entity_name}" as subject, open the rewrite with "{entity_name}", exactly as written. Otherwise, preserve the natural subject of the sentence.
2. Include only the content from claims labeled "novel".
3. Drop claims labeled "novel_trivial" or "novel_unsupported" entirely.
4. Do not add qualifiers, superlatives, editorial framing, or inferred consequences.
5. Paraphrase only where grammatically necessary to combine multiple novel claims.
6. If the novel claims are analyst estimates or forward projections, preserve the expectation framing — do not convert to past tense or disclosure language.

---

EXAMPLES:

Example 1 — entity as subject, single novel claim retained:
  Sentence: "{entity_name} launched a new AI-powered chip in partnership with TSMC, targeting data centre workloads, and now operates across 52 markets worldwide."
  Claims:
    - [novel] {entity_name} launched a new AI-powered chip in partnership with TSMC targeting data centre workloads
    - [novel_trivial] now operates across 52 markets worldwide
  Rewritten: "{entity_name} launched a new AI-powered chip in partnership with TSMC targeting data centre workloads."

Example 2 — EXTERNAL subject: original sentence opens with a market observation, not an entity action; preserve external subject
  Sentence: "Shares of {entity_name} surged 8% following the earnings report, making the stock the top performer in the S&P 500 on the day, with trading volume four times the daily average."
  Claims:
    - [novel] Shares of {entity_name} surged 8% following the earnings report
    - [novel] {entity_name} was the top performer in the S&P 500 on the day
    - [novel_trivial] trading volume was four times the daily average
  Rewritten: "Shares of {entity_name} surged 8% following the earnings report, making it the top performer in the S&P 500 on the day."
  Note: the original opens with "Shares of..." — an external market observation — so the rewrite preserves that subject. Do not rewrite as "{entity_name} has disclosed that its shares surged..."

Example 3 — EXPECTATION FRAMING: entity as subject but the novel claims are analyst estimates on future results; preserve expectation language
  Sentence: "{entity_name} is expected to report Q1 2026 EPS of $0.02 with Q2 revenue guidance of $12.7 billion and EPS of $0.15."
  Claims:
    - [novel] Analysts project {entity_name} Q1 2026 EPS of $0.02
    - [novel] Analysts project Q2 2026 revenue of $12.7 billion and EPS of $0.15
  WRONG rewrite: "{entity_name} has disclosed Q1 2026 EPS of $0.02 and Q2 revenue guidance of $12.7 billion with EPS of $0.15." ✗
  Rewritten: "{entity_name} is now expected by analysts to report Q1 2026 EPS of $0.02, with Q2 revenue projected at $12.7 billion and EPS at $0.15."

Example 4 — EXPECTATION FRAMING: entity as subject, single novel analyst consensus figure on a future period
  Sentence: "{entity_name} is forecast to generate FY2026 free cash flow of $18.5 billion, implying a 12% yield on current market cap."
  Claims:
    - [novel] Analyst consensus forecasts {entity_name} FY2026 free cash flow of $18.5 billion, implying a 12% yield
    - [novel_trivial] the company operates in more than 40 countries
  Rewritten: "{entity_name} is now forecast by analysts to generate FY2026 free cash flow of $18.5 billion, implying a 12% yield on current market cap."
  Note: "is forecast to" signals an analyst projection. Preserve that framing — do not rewrite as "has disclosed" or "has reported".

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


_REWRITE_PROMPT_PARTIAL_UPDATE = """\
You are a financial news editor. You are given a sentence about a company and a judge's analysis.

The judge found that the sentence is partially novel: the topic and surrounding context are already known from prior coverage, but the sentence adds one specific new detail — a concrete figure, a named partner, a geographic market, or a specific attribute — that does not appear in prior sources.

Your task: rewrite the sentence so the known context is a subordinate clause and the new specific detail is introduced after a pivot marker.

Structure:

    <subject>, <subordinate clause with known context>, <pivot marker> <new specific detail>.

ENTITY: "{entity_name}"

SUBJECT RULE:
Open with "{entity_name}" ONLY when the original sentence uses {entity_name} as its grammatical subject — i.e. the entity itself did, said, reported, or announced something. When the original sentence opens with an external actor (analysts, prediction markets, investors, a market metric, a regulatory body), preserve that external actor as the grammatical subject of the rewrite. The known-context → pivot → new detail structure still applies regardless.

EXPECTATION FRAMING RULE:
When the original sentence uses expectation or forecast language — "is expected to", "is projected to", "is forecast to", "analysts estimate", "analysts project", "consensus forecasts", "is anticipated to" — the new specific detail is an analyst estimate or forward projection, NOT a company disclosure. Do NOT convert it to a disclosure verb (has disclosed, has confirmed, has reported). Preserve the expectation framing using: "is now expected to", "is now projected to", "analysts now project", "is now forecast to".

PIVOT MARKERS — when the grammatical subject is {entity_name} AND the new detail is a company action or factual disclosure, use exactly one from this list:
- has now <verb> / have now <verb>
- has just <verb>
- has confirmed
- has disclosed
- has reported

When the grammatical subject is NOT {entity_name}, use a pivot that fits the actual subject naturally (e.g. "now stands at", "are now pricing", "now show"). When the new detail is an analyst estimate or forecast (even if {entity_name} is the grammatical subject), use an expectation-preserving pivot: "is now expected to", "is now projected to", "analysts now project".

RULES:
1. If the original sentence opens with "{entity_name}" as subject, open the rewrite with "{entity_name}", exactly as written. Otherwise, preserve the natural subject of the sentence.
2. Summarise the known context — what prior coverage already established about the topic — as a subordinate clause. Use past tense for the known part.
3. Place the pivot marker between the known-context clause and the new detail.
4. State the new specific detail after the pivot marker. Do not repeat the entity name after the pivot when the subject is {entity_name}.
5. If the original uses expectation language ("is expected to", "analysts project"), preserve that framing in the pivot — do not convert to a disclosure verb.
6. Do not invent or infer facts not present in the original sentence.
7. The result must be a single, coherent, publishable sentence.

---

EXAMPLES:

Example 1 — entity as subject, new earnings figure against known analyst estimates:
  Sentence: "Boeing Co. reported first quarter 2026 revenue of $22.22 billion, a 14% increase year over year, exceeding analyst estimates."
  Judge's analysis: Evidence discusses Boeing's Q1 2026 results and shows analyst consensus estimates in the $21.6–21.95B range. The actual reported figure of $22.22B, the 14% YoY growth, and the beat are not in the evidence.
  Rewritten: "Boeing Co., which faced analyst Q1 2026 revenue estimates in the $21.6–21.95 billion range, has now reported revenue of $22.22 billion, a 14% year-over-year increase that exceeded consensus."

Example 2 — entity as subject, new specific figure within a known results topic:
  Sentence: "UnitedHealth Group Inc. posted first-quarter 2026 pre-tax profit of $8.04 billion, exceeding the consensus estimate of $7.34 billion."
  Judge's analysis: Evidence covers UnitedHealth's Q1 2026 results including revenues and earnings from operations of $9.0B. The specific pre-tax profit of $8.04B and the consensus of $7.34B are not in the evidence.
  Rewritten: "UnitedHealth Group Inc., which reported Q1 2026 earnings from operations of $9.0 billion, has now disclosed a pre-tax profit of $8.04 billion, exceeding the consensus estimate of $7.34 billion."

Example 3 — entity as subject, new geographic market in a known international rollout:
  Sentence: "Amazon.com Inc. has launched Alexa+ in Spain as part of its international rollout, offering a generative AI-powered assistant that is conversational, deeply personalized, and capable of real-world actions."
  Judge's analysis: Evidence documents Alexa+ rollouts in the U.S., Mexico, Canada, UK, and Italy. A launch in Spain is not mentioned in the evidence.
  Rewritten: "Amazon.com Inc., which had already rolled out its Alexa+ generative AI assistant in the U.S., UK, Canada, Mexico, and Italy, has now launched the service in Spain."

Example 4 — entity as subject, new regulatory approval for an already-approved product:
  Sentence: "Merck & Co. Inc. received approval from the UK's Medicines and Healthcare products Regulatory Agency for Enflonsia, a vaccine to prevent respiratory syncytial virus lower respiratory tract disease in neonates and infants."
  Judge's analysis: Evidence shows Enflonsia was approved by the European Commission and in the U.S., Canada, and Switzerland. No UK MHRA approval is mentioned in the evidence.
  Rewritten: "Merck & Co. Inc., which had already received regulatory approvals for Enflonsia in the EU, U.S., Canada, and Switzerland, has now received approval from the UK's Medicines and Healthcare products Regulatory Agency."

Example 5 — entity as subject, new precise percentages beyond confirmed headline numbers:
  Sentence: "Apple Inc. achieved quarterly earnings per share of $2.84 in Q1 fiscal 2026, surpassing consensus estimates by 6.34%, and generated operating cash flow of $53.93 billion, up 80.14% year-over-year."
  Judge's analysis: Evidence confirms Apple's EPS of $2.84 and operating cash flow of ~$53.9B and that results beat consensus. The specific beat of 6.34% and YoY cash flow growth of 80.14% are not in the evidence.
  Rewritten: "Apple Inc., which reported Q1 fiscal 2026 EPS of $2.84 and operating cash flow of $53.93 billion above consensus expectations, has now confirmed that results surpassed estimates by 6.34% and that operating cash flow rose 80.14% year-over-year."

Example 6 — EXTERNAL subject: original sentence opens with analyst or market data; one new specific figure not in prior evidence
  Sentence: "The analyst consensus target price for Intel Corp. is $47.23, implying meaningful downside from current levels."
  Judge's analysis: Evidence shows prior analyst target prices for Intel in the $40–$44 range. The specific figure of $47.23 and the implied downside framing are not in the evidence.
  Rewritten: "With analyst consensus for Intel Corp. previously in the $40–$44 range, the consensus target has now been revised up to $47.23, implying meaningful downside from current levels."
  Note: the original opens with "The analyst consensus..." — an external data point — so the rewrite preserves that framing. "has now been revised" fits the implicit subject (the consensus); do not rewrite as "Intel Corp. has disclosed that analyst consensus is $47.23...".

Example 7 — EXTERNAL subject: original sentence opens with options market data; one new specific metric not in prior evidence
  Sentence: "Options market implied volatility for Intel Corp. has risen sharply ahead of earnings, now standing at 45%, well above the stock's 30-day historical average of 28%."
  Judge's analysis: Prior evidence notes elevated options activity and uncertainty for Intel ahead of earnings, but does not mention the specific implied volatility level of 45% or the comparison to the 28% historical average.
  Rewritten: "With options market implied volatility for Intel Corp. already noted as elevated ahead of earnings, it has now risen to 45% — well above the 28% historical average."
  Note: the original opens with "Options market implied volatility..." — a market pricing metric, not an entity action — so the rewrite preserves that framing. "has now risen" fits the subject "it" (the volatility); do not rewrite as "Intel Corp. has disclosed that options implied volatility stands at 45%...".

Example 8 — EXPECTATION FRAMING: entity as subject but the new detail is an analyst estimate on future results; do NOT use a disclosure verb
  Sentence: "Intel Corp. is expected to report Q1 2026 revenue of approximately $12.3 billion with EPS of $0.02, and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins."
  Judge's analysis: Prior evidence shows Intel guided for Q1 2026 revenue of $11.7–$12.7 billion. The specific figures of $12.3B Q1 revenue, $0.02 Q1 EPS, $12.7B Q2 revenue, and $0.15 Q2 EPS are Bernstein analyst projections not present in prior evidence.
  WRONG rewrite: "Intel Corp., which had guided for Q1 2026 revenue of $11.7–$12.7 billion, has disclosed Q1 EPS of $0.02 and Q2 revenue guidance of $12.7 billion with EPS of $0.15." ✗ — Intel did not disclose these; they are analyst estimates
  Rewritten: "Intel Corp., which had guided for Q1 2026 revenue of $11.7–$12.7 billion, is now projected by analysts to deliver Q1 EPS of $0.02 and Q2 revenue of $12.7 billion with EPS of $0.15, supported by higher margins."
  Note: "is expected to" in the original signals an analyst estimate on future results. Preserve that framing — do not use "has disclosed" or "has reported".

Example 9 — EXPECTATION FRAMING: entity as subject, analyst consensus price target with one new specific figure
  Sentence: "Apple Inc. is expected to generate FY2026 earnings per share of $8.12, above the prior analyst consensus of $7.94, driven by stronger services revenue."
  Judge's analysis: Prior evidence documents Apple's services growth momentum and analyst consensus EPS near $7.94. The new revised consensus figure of $8.12 EPS is not in prior evidence.
  Rewritten: "Apple Inc., which had been expected to deliver FY2026 EPS in line with the prior $7.94 consensus, is now projected by analysts to reach $8.12, driven by stronger services revenue."
  Note: both old context and new detail are analyst estimates on future results — not company disclosures. Use "is now projected to" as the pivot, not "has confirmed" or "has disclosed".

---

SENTENCE:

{sentence}

---

JUDGE'S ANALYSIS:

{reasoning}

---

OUTPUT (JSON):

{{
  "rewritten_sentence": "...",
  "reasoning": "One sentence: what was treated as known context and what was the new specific detail."
}}
"""


_REWRITE_PROMPT_MULTI_PARTIAL_UPDATE = """\
You are a financial news editor. You are given a sentence about a company and a list of its claims. The claims include two or more labeled "partially_novel": each of these touches on a topic that was already known from prior coverage, but adds one specific new detail — a concrete figure, a named partner, a geographic market, a date, or a specific attribute — that was not present in prior sources. There are no "old" claims and no "novel" claims. Any claim labeled "novel_trivial" or "novel_unsupported" must be dropped.

Your task: rewrite the sentence so the shared known baseline across all claims is expressed as a subordinate clause, and all new specific details are introduced together after a pivot marker.

Structure:

    <subject>, <subordinate clause with the shared known baseline>, <pivot marker> <all new specific details combined>.

For each "partially_novel" claim, the judge's analysis explains what was already known about the topic and what the new specific detail is. Use that reasoning to build the subordinate clause and to identify what must appear after the pivot.

ENTITY: "{entity_name}"

SUBJECT RULE:
Open with "{entity_name}" ONLY when the original sentence uses {entity_name} as its grammatical subject — i.e. the entity itself did, said, reported, or announced something. When the original sentence opens with an external actor (analysts, prediction markets, investors, a market metric, a regulatory body), preserve that external actor as the grammatical subject of the rewrite. The known-context → pivot → new details structure still applies regardless.

EXPECTATION FRAMING RULE:
When the original sentence uses expectation or forecast language — "is expected to", "is projected to", "is forecast to", "analysts estimate", "analysts project", "consensus forecasts", "is anticipated to" — the claim is an analyst estimate or forward projection, NOT a company disclosure. Do NOT convert it to a disclosure verb (has disclosed, has confirmed, has reported). Preserve the expectation framing using: "is now expected to", "is now projected to", "analysts now project", "is now forecast to".

PIVOT MARKERS — when the grammatical subject is {entity_name} AND the claims are company actions or disclosures, use exactly one from this list:
- has now <verb> / have now <verb>
- has just <verb>
- has confirmed
- has disclosed
- has reported

When the grammatical subject is NOT {entity_name}, use a pivot that fits the actual subject naturally (e.g. "are now pricing", "now stands at", "now show"). When the claims are analyst estimates or forecasts (even if {entity_name} is the grammatical subject), use an expectation-preserving pivot: "is now expected to", "is now projected to", "analysts now project".

RULES:
1. If the original sentence opens with "{entity_name}" as subject, open the rewrite with "{entity_name}", exactly as written. Otherwise, preserve the natural subject of the sentence.
2. Build the subordinate clause by synthesising the shared known baseline implied across all claims, using the judge's analysis for each. Use past tense for the known part.
3. Place the pivot marker between the known-context clause and the new material.
4. After the pivot marker, include the full substance of ALL partially_novel claims — every new specific detail. Do not omit any. Do not repeat the entity name after the pivot when the subject is {entity_name}.
5. Claims labeled "novel_trivial" or "novel_unsupported" must be dropped entirely.
6. The result must be a single, coherent, publishable sentence.

---

EXAMPLES:

Example 1 — entity as subject, two new financial metrics both above prior analyst estimates:
  Sentence: "Intel Corp. reported Q1 2026 adjusted EPS of $0.13 and revenue of $12.67 billion, both above consensus."
  Claims:
    Claim 1: Intel Corp. reported Q1 2026 adjusted EPS of $0.13
    Verdict 1: partially_novel
    Judge's analysis: Analyst EPS estimates for Q1 2026 were in the $0.08–$0.10 range. The specific reported figure of $0.13 is not in prior evidence.

    Claim 2: Intel Corp. reported Q1 2026 revenue of $12.67 billion
    Verdict 2: partially_novel
    Judge's analysis: Analyst revenue estimates for Q1 2026 were in the $12.2–$12.5 billion range. The specific reported figure of $12.67 billion is not in prior evidence.
  Rewritten: "Intel Corp., which had been expected to report Q1 2026 adjusted EPS in the $0.08–$0.10 range and revenue of approximately $12.2–$12.5 billion, has now reported EPS of $0.13 and revenue of $12.67 billion, both ahead of consensus."

Example 2 — entity as subject, two new geographic markets in a known international rollout:
  Sentence: "Microsoft Corp. has expanded its Copilot+ PC program to France and Australia, bringing its international rollout to 12 markets."
  Claims:
    Claim 1: Microsoft Corp. has expanded its Copilot+ PC program to France
    Verdict 1: partially_novel
    Judge's analysis: Microsoft's international Copilot+ PC rollout was known from prior coverage. France was not previously mentioned as a market.

    Claim 2: Microsoft Corp. has expanded its Copilot+ PC program to Australia
    Verdict 2: partially_novel
    Judge's analysis: Microsoft's international Copilot+ PC rollout was known from prior coverage. Australia was not previously mentioned as a market.
  Rewritten: "Microsoft Corp., which had been expanding its Copilot+ PC program internationally across several markets, has now added France and Australia to its rollout."

Example 3 — entity as subject, two new specific metrics in a known quarterly results context:
  Sentence: "Apple Inc. reported Q3 2026 EPS of $1.52 and services revenue of $27.3 billion, both above prior analyst estimates."
  Claims:
    Claim 1: Apple Inc. reported Q3 2026 EPS of $1.52
    Verdict 1: partially_novel
    Judge's analysis: Analyst Q3 2026 EPS estimates were in the $1.45–$1.49 range. The specific reported figure of $1.52 is not in prior evidence.

    Claim 2: Apple Inc. reported Q3 2026 services revenue of $27.3 billion
    Verdict 2: partially_novel
    Judge's analysis: Apple's services revenue growth trajectory was known from prior coverage. The specific Q3 figure of $27.3 billion is not in prior evidence.
  Rewritten: "Apple Inc., which had been expected to report Q3 2026 EPS near $1.45–$1.49 and continue its services revenue growth, has now reported EPS of $1.52 and services revenue of $27.3 billion, both above prior estimates."

Example 4 — EXTERNAL subject: sentence opens with analyst consensus data; two new specific metrics:
  Sentence: "The analyst consensus for Intel Corp. now shows a price target of $47.23 and a 12-month EPS estimate of $1.08, both revised upward."
  Claims:
    Claim 1: Analyst consensus price target for Intel Corp. is $47.23
    Verdict 1: partially_novel
    Judge's analysis: Prior analyst consensus price targets for Intel were in the $40–$44 range. The specific revised figure of $47.23 is not in prior evidence.

    Claim 2: Analyst 12-month EPS estimate for Intel Corp. is $1.08
    Verdict 2: partially_novel
    Judge's analysis: Prior analyst EPS estimates for Intel were below $1.00. The specific revised figure of $1.08 is not in prior evidence.
  Rewritten: "With analyst consensus for Intel Corp. previously showing a price target in the $40–$44 range and EPS estimates below $1.00, the consensus has now been revised to a $47.23 price target and $1.08 EPS estimate."
  Note: the original opens with analyst data — preserve the external framing. Do not rewrite as "Intel Corp. has disclosed that analyst consensus is..."

Example 5 — EXPECTATION FRAMING: entity as subject, two new analyst estimate figures on future results:
  Sentence: "Intel Corp. is expected to deliver Q2 2026 revenue of $13.1 billion and adjusted gross margin of 40.5%, above prior consensus."
  Claims:
    Claim 1: Intel Corp. is expected to deliver Q2 2026 revenue of $13.1 billion
    Verdict 1: partially_novel
    Judge's analysis: Prior consensus revenue estimates for Q2 2026 were approximately $12.5–$12.8 billion. The specific new estimate of $13.1 billion is not in prior evidence.

    Claim 2: Intel Corp. is expected to deliver Q2 2026 adjusted gross margin of 40.5%
    Verdict 2: partially_novel
    Judge's analysis: Prior consensus gross margin estimates for Q2 2026 were approximately 39%. The specific new estimate of 40.5% is not in prior evidence.
  Rewritten: "Intel Corp., which had been expected to deliver Q2 2026 revenue near $12.5–$12.8 billion and adjusted gross margin near 39%, is now projected by analysts to reach $13.1 billion in revenue and 40.5% gross margin."
  Note: both claims are analyst estimates on future results — do not use "has disclosed" or "has confirmed".

---

SENTENCE:

{sentence}

---

CLAIMS:

{claims_with_reasoning}

---

OUTPUT (JSON):

{{
  "rewritten_sentence": "...",
  "reasoning": "Brief explanation: what was synthesised as the shared known baseline and what new details were introduced."
}}
"""


_PIVOT_RELEVANCE_CHECK_PROMPT = """\
You are a financial analyst evaluating the relevance of individual news items for an investor-focused market intelligence feed. Your job is to assess how material and actionable each item is for the specified entity.

The sentence below was produced by a rewriter. It follows a fixed structure:

    {entity_name}, <subordinate clause with known context>, <pivot marker> <new specific detail>.

The pivot markers are: "has now", "has just", "has confirmed", "has disclosed", "has reported".

The subordinate clause is already known from prior coverage — it is provided only as context. Your task is to evaluate the relevance of the NEW SPECIFIC DETAIL only: the part that comes after the pivot marker. Apply the scoring criteria below to that added part, as if it were a standalone news item about {entity_name}.

Assign a relevance score from 1 (low) to 5 (high) based on how significantly the new detail affects or meaningfully relates to **{entity_name}**, considering actionability, materiality, influence, or real-world impact on the entity's status, behavior, or perception.

**Scoring criteria:**

**1 — Irrelevant**: The added detail contains no meaningful new information about {entity_name}.
  Examples: recycled data already in the subordinate clause, trivial mentions, tangential references, general market or industry commentary that does not specifically affect {entity_name}.

**2 — Barely relevant**: The added detail is weakly actionable.
  Examples: passing mentions, routine appearances, non-specific trends, minor rumors, non-material updates.

**3 — Moderately relevant**: The added detail has limited actionability or impact.
  Examples: awards, routine updates, mild public scrutiny, minor product or policy changes, small-scale incidents, class-action filings, local or short-lived effects on {entity_name}.

**4 — Relevant and actionable**: The added detail has potential material or reputational impact.
  Examples:
    • For companies/products: product launches, earnings in line with or slightly above expectations, large layoffs, notable settlements, regulatory warnings.
    • For people: major appointments, significant controversies, legally meaningful actions, endorsements with broad reach.

**5 — Highly relevant**: The added detail has substantial direct impact on {entity_name}.
  Examples:
    • For companies/products: M&A, bankruptcy, major regulatory actions, key contract wins/losses, massive surprises (earnings beats or misses, failures, breakthroughs).
    • For people: resignations or appointments to major roles, large-scale legal events, major awards or scandals with global attention.

**Additional rules:**

**Temporal relevance rule**: If the added detail is constructed entirely around past events, expired fiscal periods, or historical data — without highlighting a new development, a change, an evolution, or a meaningful comparison — it should score no higher than 2. References to past data are acceptable when used to support or contextualise something new (e.g. comparing past and present performance, framing a trajectory, or highlighting a shift). Note: the subordinate clause will always contain past context — apply this rule only to the new detail after the pivot marker.

**Date integrity rule**: If the new detail presents a fact or data point explicitly anchored to a date strictly after the current analysis date, the score must be 1 or 2, with no exceptions.

FULL SENTENCE (for context):
{rewritten_sentence}

Respond with JSON:
{{"relevance_score": <1-5>, "reason": "<one sentence focusing on why the new added detail is or is not material>"}}
"""


def run_pivot_relevance_check(
    rewritten_sentence: str,
    entity_name: str,
    llm_client,
    *,
    step_name: str,
    debug_logger=None,
    entity_metrics=None,
    default_score: int = 4,
) -> tuple[int, str | None]:
    """Score the relevance of the new specific detail in a pivot-rewritten bullet.

    Unlike the general relevance_check prompt (which evaluates the full sentence),
    this prompt instructs the LLM to focus only on the part introduced after the
    pivot marker — the genuinely new information — ignoring the known subordinate
    context clause.

    Returns (score, reason). On failure returns (default_score, None) so the bullet
    is kept rather than silently dropped.
    """
    try:
        user_content = _PIVOT_RELEVANCE_CHECK_PROMPT.format(
            entity_name=entity_name,
            rewritten_sentence=rewritten_sentence,
        )
        result = llm_client.call_with_response_format(
            system=[],
            messages=[{"role": "user", "content": user_content}],
            text_format=_NSPivotRelevanceResult,
            model=_NS_MODEL,
            max_tokens=512,
            reasoning_effort=_NS_REASONING_EFFORT,
            step_name=step_name,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
        )
        return result.relevance_score, result.reason
    except Exception:
        return default_score, None


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

    Verdicts: novel | novel_with_context | novel_noisy | partial_update_with_context | multi_partial_update | partial_update | discard_not_new | discard_unsupported
    """
    if not verdicts:
        return "old"
    labels = [v.novelty for v in verdicts]

    # Rule 1 — at least one fully novel claim
    if any(l == "novel" for l in labels):
        if all(l == "novel" for l in labels):
            return "novel"
        # Distinguish two sub-cases of "novel_with_context":
        # - mixed      : novel + old/partially_novel → rewrite with old-context clause + pivot marker
        # - novel_noisy: novel + only trivial/unsupported noise → strip noise, keep novel material
        has_old_context = any(l in ("old", "partially_novel") for l in labels)
        if not has_old_context:
            return "novel_noisy"  # rewriter strips noise, publishes novel claims as clean sentence
        return "novel_with_context"

    # Rule 2 — no fully novel, but at least one partially_novel
    if any(l == "partially_novel" for l in labels):
        pn_count = sum(l == "partially_novel" for l in labels)
        has_old = any(l == "old" for l in labels)

        if pn_count == 1 and not has_old:
            # Exactly one partially_novel claim (rest is trivial/unsupported noise that
            # the rewriter drops). Treat the known topic as subordinate context and
            # introduce the new detail after a pivot marker.
            return "partial_update"

        if has_old:
            # One or more partially_novel claims alongside at least one old claim.
            # Old claims become the subordinate context clause; partially_novel claims
            # introduce the specific new material after the pivot.
            return "partial_update_with_context"

        # Two or more partially_novel claims, no old anchor, no novel.
        # Each claim adds a specific new detail on a known topic. Rewrite by
        # synthesising the shared known baseline and introducing all new details.
        return "multi_partial_update"

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


def _ns_build_rewrite_claims_with_reasoning(
    claims: list[_NSClaim],
    claim_verdicts: list[_NSClaimVerdict],
) -> str:
    """Build the claims+verdicts+reasoning section for the multi_partial_update rewrite prompt.

    Unlike _ns_build_rewrite_claims_and_verdicts, includes the judge's per-claim
    reasoning so the rewriter can infer the known baseline for each partially_novel
    claim and construct the subordinate clause without an explicit old-claim anchor.
    """
    lines: list[str] = []
    for i, verdict in enumerate(claim_verdicts):
        idx = verdict.claim_index
        if 0 <= idx < len(claims):
            lines += [
                f"Claim {i + 1}: {claims[idx].text}",
                f"Verdict {i + 1}: {verdict.novelty}",
                f"Judge's analysis: {verdict.reasoning or '(no reasoning provided)'}",
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
                "content_diversification": {"enabled": False},
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

    # Retry transient errors with backoff, consistent with APIQueryService._call_api
    # (settings.API_RETRIES attempts, retry on HTTP error / timeout, backoff + jitter).
    # The search POST is read-only, so retrying is safe. The rate-limit hook is awaited
    # before every attempt so each retry is counted against the shared 450 QPM budget.
    data: dict | None = None
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_NS_HTTP_TIMEOUT) as client:
        for attempt in range(settings.API_RETRIES):
            if request_hook is not None:
                await request_hook()
            try:
                resp = await client.post(_NS_SEARCH_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
                last_exc = e
                logger.warning(
                    "[novelty_search] search slice error (attempt %d/%d) query=%r: %s",
                    attempt + 1,
                    settings.API_RETRIES,
                    search_query[:60],
                    e,
                )
                await asleep_with_backoff(attempt=attempt)
        else:
            raise last_exc

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

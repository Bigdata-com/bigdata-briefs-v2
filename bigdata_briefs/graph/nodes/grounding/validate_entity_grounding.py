"""
Node: entity_grounding_check

For each active bullet, runs a single LLM call to check whether the bullet
is fully grounded in its cited source references — i.e. every substantive
claim is directly supported by the source text, the bullet is about the
correct entity, and nothing is hallucinated or nonsensical.

Possible decisions per bullet:
  - VALID    → leave unchanged, ``entity_grounding.check`` is populated
  - INVALID  → mark ``is_active=False``, ``entity_grounding.check`` populated

There is no rewrite step: ungrounded bullets are discarded outright.

Service type: llm (parallel LLM calls, one per active bullet)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from jinja2 import Template
from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import (
    DECISION_INVALID,
    NODE_ENTITY_GROUNDING_CHECK,
    SERVICE_TYPE_LLM,
)
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import (
    BriefGraphState,
    BulletFailure,
    EntityGroundingBlock,
    GroundingCheckMetadata,
    NodeMetricsRecord,
    bullet_to_record,
    record_to_bullet,
)
from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
from bigdata_briefs.validation.entity_grounding import EntityGroundingResult


def classify_grounding_validity(
    state: BriefGraphState, config: RunnableConfig
) -> dict:
    """
    LangGraph node — entity_grounding_check.

    Validates each active bullet against its cited source chunks. Writes the
    ``entity_grounding.check`` sub-block onto each record. Sets
    ``is_active=False`` for INVALID bullets. No rewrite path.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    entity_name: str = state["entity_name"]
    bullet_points: list[dict] = state.get("bullet_points") or []

    active_indices = [i for i, bp in enumerate(bullet_points) if bp.get("is_active", True)]

    # ``source_references`` keys are "CQS:REF{n}" (counter-based, from
    # deduplicate_and_filter) but bullet citations use the format
    # "CQS:{document_id}-{chunk_id}" (from attribution/sources.py).
    # Build a reverse lookup keyed by the citation format so that
    # ``source_references.get(ref_id)`` finds the matching text/headline.
    raw_refs: dict = state.get("source_references") or {}
    citation_lookup: dict[str, dict] = {}
    for src in raw_refs.values():
        if isinstance(src, dict):
            doc_id = src.get("document_id")
            chunk_id = src.get("chunk_id")
            if doc_id is not None and chunk_id is not None:
                citation_lookup[f"CQS:{doc_id}-{chunk_id}"] = src

    if not active_indices or not citation_lookup:
        wall_ms = (time.monotonic() - t0) * 1000
        return {
            "node_metrics": [
                NodeMetricsRecord(
                    node_id=NODE_ENTITY_GROUNDING_CHECK,
                    service_type=SERVICE_TYPE_LLM,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    wall_time_ms=wall_ms,
                    extra={"skipped": True, "reason": "no active bullets or no source_references"},
                ).model_dump()
            ]
        }

    prompt_keys = get_prompt_keys("entity_grounding_check")

    def check_single(bullet_idx: int, bullet_text: str, citations: list[str]):
        """Run one LLM call for a single bullet."""
        ref_texts = []
        ref_headlines = []
        for ref_id in citations:
            src = citation_lookup.get(ref_id) or {}
            ref_texts.append(src.get("text") or "")
            ref_headlines.append(src.get("headline") or "")

        if not any(ref_texts):
            # No references at all → cannot be grounded. Discard defensively.
            return bullet_idx, "invalid", "no reference text available"

        system_prompt = Template(prompt_keys.system_prompt).render(entity_name=entity_name)
        user_prompt = prompt_keys.user_template.render(
            entity_name=entity_name,
            bullet_point=bullet_text,
            reference_texts=ref_texts,
            reference_headlines=ref_headlines,
            references=citations,
            response_format=f"{EntityGroundingResult.model_json_schema()}",
        )
        result: EntityGroundingResult = deps.llm_client.call_with_response_format(
            system=[{"role": "system", "content": system_prompt}],
            messages=[{"role": "user", "content": user_prompt}],
            text_format=EntityGroundingResult,
            step_name=f"entity_grounding_check_{bullet_idx}",
            debug_logger=deps.debug_logger,
            entity_metrics=deps.entity_metrics,
            **prompt_keys.llm_kwargs,
        )
        if result is None:
            # LLM returned nothing parseable — be conservative and discard.
            return bullet_idx, "invalid", "grounding check returned no result"
        decision = result.decision.lower().strip()
        # Only valid/invalid are accepted; anything else collapses to invalid.
        if decision != "valid":
            decision = "invalid"
        return bullet_idx, decision, result.reason

    # Run in parallel
    results: dict[int, tuple[str, str]] = {}
    failures: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                check_single,
                i,
                bullet_points[i]["text"],
                bullet_points[i].get("citations") or [],
            ): i
            for i in active_indices
        }
        for future in as_completed(futures):
            bidx = futures[future]
            try:
                bidx, decision, reason = future.result()
                results[bidx] = (decision, reason)
            except Exception as e:
                failures[bidx] = e

    # Apply results
    updated = list(bullet_points)
    valid_count = invalid_count = 0

    for i in active_indices:
        record = bullet_to_record(updated[i])

        if i in failures:
            e = failures[i]
            record.is_active = False
            record.failure = BulletFailure(
                node_id=NODE_ENTITY_GROUNDING_CHECK,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            updated[i] = record_to_bullet(record)
            continue

        decision, reason = results.get(i, ("valid", ""))
        record.entity_grounding = EntityGroundingBlock(
            check=GroundingCheckMetadata(
                decision=decision,
                reason=reason,
            )
        )
        if decision == DECISION_INVALID:
            record.is_active = False
            invalid_count += 1
        else:
            valid_count += 1

        updated[i] = record_to_bullet(record)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_ENTITY_GROUNDING_CHECK,
        service_type=SERVICE_TYPE_LLM,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        llm_calls=len(active_indices),
        extra={
            "valid": valid_count,
            "invalid": invalid_count,
            "failed_bullets": len(failures),
        },
    )

    return {
        "bullet_points": updated,
        "node_metrics": [metrics.model_dump()],
    }

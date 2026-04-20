"""
Entity Grounding Validator.

Validates that bullet points are properly supported by references 
that explicitly mention the entity, not generic references that 
could refer to other entities.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from jinja2 import Template
from pydantic import BaseModel, Field

from bigdata_briefs import logger
from bigdata_briefs.validation.base import (
    BaseValidator,
    ValidationAction,
    ValidationActionItem,
)

if TYPE_CHECKING:
    from bigdata_briefs.debug_logger import DebugLogger
    from bigdata_briefs.llm_client import LLMClient
    from bigdata_briefs.metrics import EntityStepMetrics


class EntityGroundingResult(BaseModel):
    """Result of entity grounding check for a single bullet."""

    decision: str = Field(
        description="Decision: 'VALID' or 'INVALID'"
    )
    reason: str = Field(
        description="1-2 sentence explanation of the decision"
    )


class EntityGroundingValidator(BaseValidator):
    """
    Validates that bullet points are supported by references
    that explicitly mention the entity.

    Problem this solves:
    - Chunks may contain generic references like "the company"
    - Headlines may mention different entities than the text
    - LLM may incorrectly attribute info to wrong entity

    Processing:
    - Each bullet is validated INDIVIDUALLY and in PARALLEL
    - Actions: KEEP, DISCARD (no REWRITE, no MERGE)
    """
    
    name = "entity_grounding"
    
    def __init__(self, llm_client: "LLMClient", max_workers: int = 10):
        super().__init__(llm_client)
        self.max_workers = max_workers
    
    def run(
        self,
        bullets: list[str],
        references: list[list[str]],
        reference_texts: list[list[str]],
        reference_headlines: list[list[str]],
        entity_name: str,
        scores: list[int] | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[list[str], list[list[str]], list[int] | None, list[int]]:
        """
        Validate all bullets against their references.
        
        Args:
            bullets: List of bullet point texts
            references: List of reference ID lists for each bullet
            reference_texts: List of reference text lists for each bullet
            reference_headlines: List of headline lists for each bullet
            entity_name: Name of the entity being reported on
            scores: Optional list of relevance scores for each bullet
            debug_logger: Optional debug logger for saving LLM calls
            entity_metrics: Optional metrics tracker for cost tracking
            
        Returns:
            Tuple of (validated_bullets, validated_references, validated_scores,
            source_input_indices). ``source_input_indices[k]`` is the input row index
            for ``validated_bullets[k]`` (stable alignment for pipeline checkpoints).
        """
        if not bullets:
            return bullets, references, scores, []
        
        logger.info(
            f"[{self.name}] Validating {len(bullets)} bullets for entity: {entity_name}"
        )
        
        # Step 1: Identify actions in parallel (one LLM call per bullet)
        actions = self._identify_all_parallel(
            bullets, references, reference_texts, reference_headlines, entity_name,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
        )
        
        # Step 2: Execute actions
        return self._execute_actions(
            bullets,
            references,
            actions,
            entity_name,
            scores,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
        )
    
    def _identify_all_parallel(
        self,
        bullets: list[str],
        references: list[list[str]],
        reference_texts: list[list[str]],
        reference_headlines: list[list[str]],
        entity_name: str,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> list[ValidationActionItem | None]:
        """
        Identify actions for all bullets in parallel.
        
        Returns:
            List of ValidationActionItem (or None for KEEP)
        """
        actions: list[ValidationActionItem | None] = [None] * len(bullets)
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            
            for i, (bullet, refs, texts, headlines) in enumerate(
                zip(bullets, references, reference_texts, reference_headlines)
            ):
                # Skip if no references to validate
                if not refs or not texts:
                    continue
                
                future = executor.submit(
                    self._identify_single,
                    index=i,
                    bullet=bullet,
                    references=refs,
                    reference_texts=texts,
                    reference_headlines=headlines,
                    entity_name=entity_name,
                    debug_logger=debug_logger,
                    entity_metrics=entity_metrics,
                )
                futures[future] = i
            
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    action = future.result()
                    if action and action.action != ValidationAction.KEEP:
                        actions[idx] = action
                except Exception as e:
                    _model = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
                    logger.error(
                        "[entity_grounding] Error validating bullet | entity=%s bullet_index=%s model=%s | %s",
                        entity_name,
                        idx,
                        _model,
                        e,
                    )
        
        # Log summary
        discard_count = sum(1 for a in actions if a and a.action == ValidationAction.DISCARD)
        keep_count = len(bullets) - discard_count

        logger.info(
            f"[{self.name}] Validation results: {keep_count} KEEP, "
            f"{discard_count} DISCARD"
        )

        return actions
    
    def _identify_single(
        self,
        index: int,
        bullet: str,
        references: list[str],
        reference_texts: list[str],
        reference_headlines: list[str],
        entity_name: str,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> ValidationActionItem | None:
        """
        Validate a single bullet against its references.
        
        Returns:
            ValidationActionItem or None if KEEP
        """
        from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
        
        prompt_keys = get_prompt_keys("entity_grounding_check")
        
        system_prompt = Template(prompt_keys.system_prompt).render(
            entity_name=entity_name
        )
        
        user_prompt = prompt_keys.user_template.render(
            entity_name=entity_name,
            bullet_point=bullet,
            reference_texts=reference_texts,
            reference_headlines=reference_headlines,
            references=references,
            response_format=f"{EntityGroundingResult.model_json_schema()}",
        )
        
        messages = [{"role": "user", "content": user_prompt}]
        
        try:
            result: EntityGroundingResult | None = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=EntityGroundingResult,
                step_name=f"{self.name}_check_{index}",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            if result is None:
                _model = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
                _step = f"llm_{self.name}_check_{index}"
                _slug = re.sub(r"_+", "_", re.sub(r"[^\w\-]", "_", entity_name)).strip("_") or "entity"
                logger.error(
                    "[entity_grounding] LLM returned None | entity=%s bullet_index=%s model=%s "
                    "debug_file=run_%s/debug_logs/%s/*/.../03_entity_grounding/%s.json",
                    entity_name,
                    index,
                    _model,
                    _model,
                    _slug,
                    _step,
                )
                return None
            return self._parse_result(index, result, references)

        except Exception as e:
            _model = os.environ.get("BRIEFS_DEFAULT_MODEL") or "?"
            _step = f"llm_{self.name}_check_{index}"
            _slug = re.sub(r"_+", "_", re.sub(r"[^\w\-]", "_", entity_name)).strip("_") or "entity"
            _debug_path = (
                f"run_{_model}/debug_logs/{_slug}/*/iterative_sequential_with_thematic_chunks/"
                f"details/03_entity_grounding/{_step}.json"
            )
            logger.error(
                "[entity_grounding] Error checking bullet | entity=%s bullet_index=%s model=%s "
                "debug_file=%s | %s",
                entity_name,
                index,
                _model,
                _debug_path,
                e,
            )
            return None  # Keep on error
    
    def _parse_result(
        self,
        index: int,
        result: EntityGroundingResult,
        references: list[str],
    ) -> ValidationActionItem | None:
        """Parse LLM result into ValidationActionItem."""

        decision = result.decision.lower().strip()

        if decision == "valid":
            return None  # KEEP

        # Anything other than "valid" is treated as INVALID → DISCARD.
        return ValidationActionItem(
            index=str(index),
            action=ValidationAction.DISCARD,
            rationale=result.reason,
        )
    
    def _execute_actions(
        self,
        bullets: list[str],
        references: list[list[str]],
        actions: list[ValidationActionItem | None],
        entity_name: str,
        scores: list[int] | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[list[str], list[list[str]], list[int] | None, list[int]]:
        """
        Execute validation actions.

        DISCARD: Skip bullet entirely
        KEEP (None): Keep as-is

        Returns:
            Tuple of (result_bullets, result_refs, result_scores, source_input_indices)
        """
        result_bullets: list[str] = []
        result_refs: list[list[str]] = []
        result_scores: list[int] = []
        source_input_indices: list[int] = []

        for i, (bullet, refs, action) in enumerate(zip(bullets, references, actions)):
            score = scores[i] if scores and i < len(scores) else None

            if action is None:
                # KEEP
                result_bullets.append(bullet)
                result_refs.append(refs)
                source_input_indices.append(i)
                if score is not None:
                    result_scores.append(score)

            elif action.action == ValidationAction.DISCARD:
                logger.info(
                    f"[{self.name}] Discarding bullet {i}: {action.rationale[:80]}..."
                )
                continue  # Skip this bullet (don't add score)

        logger.info(
            f"[{self.name}] Final: {len(result_bullets)} bullets "
            f"(from {len(bullets)} original)"
        )

        # Return None for scores if input was None
        final_scores = result_scores if scores is not None else None

        return result_bullets, result_refs, final_scores, source_input_indices


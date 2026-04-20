"""
Base classes for the validation system.

Provides abstract BaseValidator class and common models used by all validators.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

from jinja2 import Template
from pydantic import BaseModel, Field

from bigdata_briefs import logger

if TYPE_CHECKING:
    from bigdata_briefs.debug_logger import DebugLogger
    from bigdata_briefs.llm_client import LLMClient
    from bigdata_briefs.metrics import EntityStepMetrics


class ValidationAction(StrEnum):
    """Actions that a validator can take on a bullet point."""
    KEEP = "keep"           # Keep bullet as-is
    DISCARD = "discard"     # Remove bullet
    REWRITE = "rewrite"     # Rewrite bullet (remove invalid parts)
    MERGE = "merge"         # Merge with other bullets


class ValidationActionItem(BaseModel):
    """Single action identified by a validator's identify step."""
    
    index: str = Field(
        description="Index of the bullet point (e.g., '0', '1', 'S0', 'C1')."
    )
    action: ValidationAction = Field(
        description="Action to take on this bullet point."
    )
    merge_with: list[str] = Field(
        default_factory=list,
        description="For MERGE action: list of bullet indices to merge with."
    )
    rationale: str = Field(
        description="Explanation of why this action was chosen."
    )
    remove_references: list[str] = Field(
        default_factory=list,
        description="For REWRITE action: list of reference IDs to remove."
    )


class ValidationPlan(BaseModel):
    """Plan containing all actions identified by a validator."""
    
    actions: list[ValidationActionItem] = Field(
        default_factory=list,
        description="List of actions for bullets that need changes. Bullets not listed are kept as-is."
    )


class RewrittenBulletResult(BaseModel):
    """Result of rewriting a bullet point."""
    
    rewritten_text: str = Field(
        description="The rewritten bullet point text."
    )


class MergedBulletResult(BaseModel):
    """Result of merging bullet points."""
    
    merged_text: str = Field(
        description="The merged bullet point text."
    )


class BaseValidator(ABC):
    """
    Abstract base class for all validators.
    
    Each validator implements its own run() method with custom parameters,
    but shares generic call_rewrite() and call_merge() methods.
    
    Typical validator flow:
    1. identify() - Validator-specific logic to determine actions (KEEP/DISCARD/REWRITE/MERGE)
    2. execute() - Apply actions using generic rewrite/merge prompts
    """
    
    name: str = "base"
    
    def __init__(self, llm_client: "LLMClient"):
        self.llm_client = llm_client
    
    @abstractmethod
    def run(self, **kwargs) -> tuple[list[str], list[list[str]]]:
        """
        Execute the validation.
        
        Each validator implements this with its own parameters.
        
        Returns:
            Tuple of (processed_bullets, processed_references)
        """
        pass
    
    def call_rewrite(
        self,
        bullet: str,
        rationale: str,
        entity_name: str,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> str:
        """
        Generic rewrite using validator_rewrite prompt.
        
        Args:
            bullet: Original bullet text
            rationale: Reason for rewrite (what to remove/keep)
            entity_name: Name of the entity
            debug_logger: Optional debug logger for saving LLM calls
            entity_metrics: Optional metrics tracker for cost tracking
            
        Returns:
            Rewritten bullet text
        """
        from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
        
        prompt_keys = get_prompt_keys("validator_rewrite")
        
        system_prompt = Template(prompt_keys.system_prompt).render(
            entity_name=entity_name
        )
        
        user_prompt = prompt_keys.user_template.render(
            entity_name=entity_name,
            original_bullet=bullet,
            rationale=rationale,
            response_format=f"{RewrittenBulletResult.model_json_schema()}",
        )
        
        messages = [{"role": "user", "content": user_prompt}]
        
        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=RewrittenBulletResult,
                step_name=f"{self.name}_rewrite",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            return result.rewritten_text
        except Exception as e:
            logger.error(f"[{self.name}] Rewrite failed: {e}")
            return bullet  # Return original on failure
    
    def call_merge(
        self,
        bullets: list[str],
        rationale: str,
        entity_name: str,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> str:
        """
        Generic merge using validator_merge prompt.
        
        Args:
            bullets: List of bullets to merge
            rationale: Reason for merging
            entity_name: Name of the entity
            debug_logger: Optional debug logger for saving LLM calls
            entity_metrics: Optional metrics tracker for cost tracking
            
        Returns:
            Merged bullet text
        """
        from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
        
        prompt_keys = get_prompt_keys("validator_merge")
        
        system_prompt = Template(prompt_keys.system_prompt).render(
            entity_name=entity_name
        )
        
        user_prompt = prompt_keys.user_template.render(
            entity_name=entity_name,
            bullets_to_merge=bullets,
            rationale=rationale,
            response_format=f"{MergedBulletResult.model_json_schema()}",
        )
        
        messages = [{"role": "user", "content": user_prompt}]
        
        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=MergedBulletResult,
                step_name=f"{self.name}_merge",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            return result.merged_text
        except Exception as e:
            logger.error(f"[{self.name}] Merge failed: {e}")
            return bullets[0] if bullets else ""  # Return first on failure


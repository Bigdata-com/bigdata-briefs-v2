"""
Validation system for bullet point processing.

This module provides a generic validation framework that can be extended
with custom validators for different validation needs.
"""

from bigdata_briefs.validation.base import (
    BaseValidator,
    ValidationAction,
    ValidationActionItem,
    ValidationPlan,
    RewrittenBulletResult,
    MergedBulletResult,
)
from bigdata_briefs.validation.entity_grounding import EntityGroundingValidator

__all__ = [
    "BaseValidator",
    "ValidationAction",
    "ValidationActionItem",
    "ValidationPlan",
    "RewrittenBulletResult",
    "MergedBulletResult",
    "EntityGroundingValidator",
]


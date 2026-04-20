import os
from typing import Literal

import yaml
from jinja2 import StrictUndefined, Template

from bigdata_briefs.models import PromptConfig

base_dir = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(base_dir, "prompts.yaml")

# In prompts.yaml, "default_model" is a reserved top-level key. Steps may omit "model"
# to use it, or set "model" to override (e.g. gpt-4.1 for bullet gen + novelty Step 1 judge).
#
# When BRIEFS_DEFAULT_MODEL is set (e.g. in three_models_parallel runs), only these
# prompts use that model; all other prompts use BRIEFS_OTHER_STEPS_MODEL (gpt-4.1-mini)
# or their step-level override in YAML.
BRIEFS_RUN_MODEL_PROMPTS = frozenset({
    "entity_update_iterative_by_theme",  # bullet generation
    "novelty_embedding_evaluation_prompt",  # embedding novelty: classify KEEP/DISCARD/REWRITE
})
# Model used for all steps that are not in BRIEFS_RUN_MODEL_PROMPTS when running
# three-model comparison (BRIEFS_DEFAULT_MODEL set).
BRIEFS_OTHER_STEPS_MODEL = "gpt-4.1-mini"

# Reasoning models (e.g. gpt-5-mini) do not support temperature; use reasoning_effort
# in model_kwargs instead (e.g. reasoning_effort: "low"). Standard models use temperature.
REASONING_MODELS = frozenset({"gpt-5-mini"})


def get_prompt_keys(
    prompt_name: Literal[
        "concept_extraction",
        "entity_update_from_concepts",
        "entity_update_iterative_by_theme",
        "relevance_check",
        "novelty_embedding_evaluation_prompt",
        "novelty_embedding_rewrite_prompt",
        "thematic_clustering",
        "consolidate_theme",
        "thematic_clustering_aggressive",
        "consolidate_theme_aggressive",
        "cleanup_identify",
        "cleanup_merge",
        "cleanup_rewrite",
        "dedup_identify",
        "dedup_merge",
        "dedup_rewrite",
        # Generic validator prompts
        "validator_rewrite",
        "validator_merge",
        # Entity grounding validator
        "entity_grounding_check",
    ],
) -> PromptConfig:
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    default_model = data.get("default_model", "gpt-4o-mini")
    # Runtime override: when BRIEFS_DEFAULT_MODEL is set, only RUN_MODEL prompts use it
    env_model = os.environ.get("BRIEFS_DEFAULT_MODEL")
    properties = data[prompt_name]
    system_prompt = properties.pop("system_prompt")
    user_template = properties.pop("user_template")
    if env_model and prompt_name in BRIEFS_RUN_MODEL_PROMPTS:
        model = env_model
    else:
        # In three-model runs, other steps use gpt-4.1-mini; otherwise YAML default/override
        fallback = BRIEFS_OTHER_STEPS_MODEL if env_model else default_model
        model = properties.get("model", fallback)
    raw_kwargs = properties["model_kwargs"].copy()
    if model in REASONING_MODELS:
        raw_kwargs.pop("temperature", None)
        raw_kwargs.setdefault("reasoning_effort", "low")
    else:
        raw_kwargs.pop("reasoning_effort", None)
    llm_kwargs = {**raw_kwargs, "model": model}
    return PromptConfig(
        system_prompt=system_prompt,
        user_template=Template(user_template, undefined=StrictUndefined),
        llm_kwargs=llm_kwargs,
    )

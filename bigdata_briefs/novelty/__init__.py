"""Novelty filtering and multi-evaluator novelty check.

Import concrete APIs from submodules, e.g. ``bigdata_briefs.novelty.novelty_service``.
This package ``__init__`` stays free of eager imports so ``models`` can load
``bullet_pipeline_checkpoint`` without pulling in ``evaluators`` (which depend on ``models``).
"""

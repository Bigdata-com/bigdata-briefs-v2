"""
DEPRECATED — run_search_novelty.py

This module has been replaced by four separate LangGraph nodes:

  1. parse_and_plan_search.py   → NODE_NOVELTY_SEARCH_PARSE_AND_PLAN
  2. fetch_search_evidence.py   → NODE_NOVELTY_SEARCH_FETCH
  3. judge_novelty_by_search.py → NODE_NOVELTY_SEARCH_JUDGMENT
  4. rewrite_search_bullets.py  → NODE_NOVELTY_SEARCH_REWRITE

Shared implementation (models, prompts, helpers, search functions) lives in
_search_impl.py.

Intermediate data between nodes flows through deps._search_cache (keyed by
trace_id), mirroring the deps._embedding_cache pattern used in the novelty
embedding phase.

This file is intentionally empty of active code.  It will be removed in a
future cleanup pass.
"""

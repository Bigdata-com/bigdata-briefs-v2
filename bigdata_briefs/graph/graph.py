"""
Brief 2.0 LangGraph Pipeline

Composes all canonical nodes into a StateGraph.

Graph topology:

  START → initialize_pipeline → initial_check
    ─[no_data]────────────────────────────────────────────────────────→ END
    ─[continue]→ exploratory_search
      ─[no_data]──────────────────────────────────────────────────────→ END
      ─[continue]→ quarter_info → concept_extraction → concept_search
        → concept_search_postprocessing
          ─[no_data]────────────────────────────────────────────────→ END
          ─[continue]→ [Bullets_Generation_and_Scoring subgraph]
            ─[no_data]──────────────────────────────────────────────→ END
            ─[continue]→ entity_grounding_check
              → embed_and_retrieve
              → novelty_judgment_embedding
              → persist_novel_embeddings          ← rewrite_embedding and
              → novelty_search_parse_and_plan       relevance_check_embedding
              → novelty_search_fetch                removed (see note below)
              → novelty_search_judgment
              → novelty_search_rewrite
              → relevance_score_search
              → save_novel_bullets
                ─[no_data]────────────────────────────────────────→ END
                ─[post_processing]→ redundancy_check
                  → thematic_consolidation
                  → standalone_validation
                  → build_report → END
                ─[build_report]─────────────────────────────────→ build_report → END

NOTE — rewrite_embedding and relevance_check_embedding are disabled:
  The embedding novelty check exists solely to decide keep / discard / rewrite.
  Bullets marked "discard" are deactivated immediately by novelty_judgment_embedding.
  Bullets marked "rewrite" (partially novel) are kept active with their original text
  and passed directly to the novelty-via-search phase, which judges them with real
  evidence and applies the appropriate pivot-structure rewrite. Running the embedding
  rewriter first would produce an intermediate rewritten text that the search phase
  would then re-judge and potentially rewrite again — wasted LLM calls with no benefit.
  The nodes still exist in the codebase but are not wired into the graph.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from bigdata_briefs.graph.constants import (
    NODE_BUILD_REPORT,
    NODE_CONCEPT_EXTRACTION,
    NODE_CONCEPT_SEARCH,
    NODE_CONCEPT_SEARCH_POSTPROCESSING,
    NODE_EMBED_AND_RETRIEVE,
    NODE_ENTITY_GROUNDING_CHECK,
    NODE_EXPLORATORY_SEARCH,
    NODE_INITIAL_CHECK,
    NODE_INITIALIZE_PIPELINE,
    NODE_NOVELTY_JUDGMENT_EMBEDDING,
    NODE_NOVELTY_SEARCH_FETCH,
    NODE_NOVELTY_SEARCH_JUDGMENT,
    NODE_NOVELTY_SEARCH_PARSE_AND_PLAN,
    NODE_NOVELTY_SEARCH_REWRITE,
    NODE_PERSIST_NOVEL_EMBEDDINGS,
    NODE_QUARTER_INFO,
    NODE_REDUNDANCY_CHECK,
    NODE_RELEVANCE_SCORE_SEARCH,
    NODE_SAVE_NOVEL_BULLETS,
    NODE_STANDALONE_VALIDATION,
    NODE_THEMATIC_CONSOLIDATION,
    PIPELINE_STATUS_NO_DATA,
    ROUTE_BUILD_REPORT,
    ROUTE_CONTINUE,
    ROUTE_NO_DATA,
    ROUTE_POST_PROCESSING,
    SUBGRAPH_BULLET_GENERATION,
)
from bigdata_briefs.graph.nodes.initialize.initialize_pipeline import initialize_pipeline
from bigdata_briefs.graph.nodes.grounding.validate_entity_grounding import (
    classify_grounding_validity,
)
from bigdata_briefs.graph.nodes.novelty_embedding.embed_and_retrieve_candidates import (
    compute_embeddings_and_retrieve_candidates,
)
from bigdata_briefs.graph.nodes.novelty_embedding.judge_novelty_by_embedding import (
    evaluate_novelty_by_embedding_similarity,
)
from bigdata_briefs.graph.nodes.novelty_embedding.persist_novel_embeddings import (
    persist_embeddings_of_novel_bullets,
)
# rewrite_non_novel_bullets and check_rewrite_relevance not imported:
# those nodes are disabled (see module docstring).
from bigdata_briefs.graph.nodes.novelty_search.check_search_rewrite_relevance import (
    score_search_rewrite_relevance,
)
from bigdata_briefs.graph.nodes.novelty_search.parse_and_plan_search import (
    parse_and_plan_search,
)
from bigdata_briefs.graph.nodes.novelty_search.fetch_search_evidence import (
    fetch_search_evidence,
)
from bigdata_briefs.graph.nodes.novelty_search.judge_novelty_by_search import (
    judge_novelty_by_search,
)
from bigdata_briefs.graph.nodes.novelty_search.rewrite_search_bullets import (
    rewrite_search_bullets,
)
from bigdata_briefs.graph.nodes.phase1_search.check_entity_data import (
    verify_entity_has_search_results,
)
from bigdata_briefs.graph.nodes.phase1_search.deduplicate_and_filter import (
    deduplicate_and_filter_concept_results,
)
from bigdata_briefs.graph.nodes.phase1_search.extract_concepts import (
    extract_thematic_concepts_from_chunks,
)
from bigdata_briefs.graph.nodes.phase1_search.fetch_quarter_info import (
    resolve_fiscal_quarter_from_calendar,
)
from bigdata_briefs.graph.nodes.phase1_search.run_exploratory_search import (
    execute_broad_topic_search,
)
from bigdata_briefs.graph.nodes.phase1_search.search_by_concepts import (
    execute_parallel_concept_queries,
)
from bigdata_briefs.graph.nodes.post_processing.check_bullet_redundancy import (
    detect_and_merge_redundant_bullets,
)
from bigdata_briefs.graph.nodes.post_processing.consolidate_themes import (
    cluster_and_consolidate_by_theme,
)
from bigdata_briefs.graph.nodes.post_processing.validate_standalone_bullets import (
    evaluate_standalone_bullet_actions,
)
from bigdata_briefs.graph.nodes.reconcile.save_novel_bullets import (
    save_novel_bullet_points,
)
from bigdata_briefs.graph.output.build_entity_report import (
    assemble_single_entity_report,
)
from bigdata_briefs.graph.node_logger import with_node_log
from bigdata_briefs.graph.state import BriefGraphState
from bigdata_briefs.graph.subgraphs.bullet_generation_loop import (
    compile_bullet_generation_subgraph,
)
from bigdata_briefs.settings import settings


# ── Conditional edge functions ─────────────────────────────────────────────────


def _route_initial_check(state: BriefGraphState) -> str:
    """Route to END when no data found, else continue to exploratory search."""
    if state.get("pipeline_status") == PIPELINE_STATUS_NO_DATA:
        return ROUTE_NO_DATA
    return ROUTE_CONTINUE


def _route_exploratory_search(state: BriefGraphState) -> str:
    """Route to END when exploratory search returns no chunks."""
    chunks = state.get("exploratory_chunks") or []
    if not chunks:
        return ROUTE_NO_DATA
    return ROUTE_CONTINUE


def _route_concept_postprocessing(state: BriefGraphState) -> str:
    """Route to END when concept search produced no usable chunks."""
    processed = state.get("processed_concept_results") or {}
    results = processed.get("results_by_theme") or {}
    has_results = any(len(v) > 0 for v in results.values()) if results else False
    if not has_results:
        return ROUTE_NO_DATA
    return ROUTE_CONTINUE


def _route_after_bullet_subgraph(state: BriefGraphState) -> str:
    """Route to END if no active bullets survived generation + relevance scoring."""
    bullet_points = state.get("bullet_points") or []
    active = sum(1 for bp in bullet_points if bp.get("is_active", True))
    if active == 0:
        return ROUTE_NO_DATA
    return ROUTE_CONTINUE


def _route_save_novel_bullets(state: BriefGraphState) -> str:
    """
    After save_novel_bullets, decide post-processing vs build_report vs END.

    - no_data       → END (nothing survived the full novelty pipeline)
    - post_processing → redundancy_check (when ENABLE_BULLET_PROCESSING_PHASE=True)
    - build_report  → skip post-processing, go straight to report assembly
    """
    if state.get("pipeline_status") == PIPELINE_STATUS_NO_DATA:
        return ROUTE_NO_DATA
    if settings.ENABLE_BULLET_PROCESSING_PHASE:
        return ROUTE_POST_PROCESSING
    return ROUTE_BUILD_REPORT


# ── Graph builder ──────────────────────────────────────────────────────────────


def build_brief_graph() -> StateGraph:
    """
    Construct and return the Brief 2.0 StateGraph (uncompiled).

    Call ``.compile()`` on the returned graph to get an executable graph.
    """
    g = StateGraph(BriefGraphState)

    L = with_node_log  # shorthand

    # ── Initialization ───────────────────────────────────────────────────────
    g.add_node(NODE_INITIALIZE_PIPELINE, L(NODE_INITIALIZE_PIPELINE, initialize_pipeline))

    # ── Phase 1: Search ──────────────────────────────────────────────────────
    g.add_node(NODE_INITIAL_CHECK, L(NODE_INITIAL_CHECK, verify_entity_has_search_results))
    g.add_node(NODE_EXPLORATORY_SEARCH, L(NODE_EXPLORATORY_SEARCH, execute_broad_topic_search))
    g.add_node(NODE_QUARTER_INFO, L(NODE_QUARTER_INFO, resolve_fiscal_quarter_from_calendar))
    g.add_node(NODE_CONCEPT_EXTRACTION, L(NODE_CONCEPT_EXTRACTION, extract_thematic_concepts_from_chunks))
    g.add_node(NODE_CONCEPT_SEARCH, L(NODE_CONCEPT_SEARCH, execute_parallel_concept_queries))
    g.add_node(NODE_CONCEPT_SEARCH_POSTPROCESSING, L(NODE_CONCEPT_SEARCH_POSTPROCESSING, deduplicate_and_filter_concept_results))

    # ── Phase 2: Bullet Generation subgraph (has its own internal node logs) ─
    bullet_subgraph = compile_bullet_generation_subgraph()
    g.add_node(SUBGRAPH_BULLET_GENERATION, bullet_subgraph)

    # ── Grounding ─────────────────────────────────────────────────────────────
    g.add_node(NODE_ENTITY_GROUNDING_CHECK, L(NODE_ENTITY_GROUNDING_CHECK, classify_grounding_validity))

    # ── Novelty Embedding ────────────────────────────────────────────────────
    g.add_node(NODE_EMBED_AND_RETRIEVE, L(NODE_EMBED_AND_RETRIEVE, compute_embeddings_and_retrieve_candidates))
    g.add_node(NODE_NOVELTY_JUDGMENT_EMBEDDING, L(NODE_NOVELTY_JUDGMENT_EMBEDDING, evaluate_novelty_by_embedding_similarity))
    # NODE_REWRITE_EMBEDDING and NODE_RELEVANCE_CHECK_EMBEDDING intentionally omitted:
    # the embedding phase only discards old bullets; partially-novel ones go to novelty
    # search with their original text so the search phase can judge and rewrite them.
    g.add_node(NODE_PERSIST_NOVEL_EMBEDDINGS, L(NODE_PERSIST_NOVEL_EMBEDDINGS, persist_embeddings_of_novel_bullets))

    # ── Novelty via Search ───────────────────────────────────────────────────
    g.add_node(NODE_NOVELTY_SEARCH_PARSE_AND_PLAN, L(NODE_NOVELTY_SEARCH_PARSE_AND_PLAN, parse_and_plan_search))
    g.add_node(NODE_NOVELTY_SEARCH_FETCH, L(NODE_NOVELTY_SEARCH_FETCH, fetch_search_evidence))
    g.add_node(NODE_NOVELTY_SEARCH_JUDGMENT, L(NODE_NOVELTY_SEARCH_JUDGMENT, judge_novelty_by_search))
    g.add_node(NODE_NOVELTY_SEARCH_REWRITE, L(NODE_NOVELTY_SEARCH_REWRITE, rewrite_search_bullets))
    g.add_node(NODE_RELEVANCE_SCORE_SEARCH, L(NODE_RELEVANCE_SCORE_SEARCH, score_search_rewrite_relevance))

    # ── Finalization ──────────────────────────────────────────────────────────
    g.add_node(NODE_SAVE_NOVEL_BULLETS, L(NODE_SAVE_NOVEL_BULLETS, save_novel_bullet_points))

    # ── Post-Processing (optional) ───────────────────────────────────────────
    g.add_node(NODE_REDUNDANCY_CHECK, L(NODE_REDUNDANCY_CHECK, detect_and_merge_redundant_bullets))
    g.add_node(NODE_THEMATIC_CONSOLIDATION, L(NODE_THEMATIC_CONSOLIDATION, cluster_and_consolidate_by_theme))
    g.add_node(NODE_STANDALONE_VALIDATION, L(NODE_STANDALONE_VALIDATION, evaluate_standalone_bullet_actions))

    # ── Output ────────────────────────────────────────────────────────────────
    g.add_node(NODE_BUILD_REPORT, L(NODE_BUILD_REPORT, assemble_single_entity_report))

    # ── Edges ─────────────────────────────────────────────────────────────────

    # START → initialize_pipeline → initial_check
    g.add_edge(START, NODE_INITIALIZE_PIPELINE)
    g.add_edge(NODE_INITIALIZE_PIPELINE, NODE_INITIAL_CHECK)

    # initial_check (conditional)
    g.add_conditional_edges(
        NODE_INITIAL_CHECK,
        _route_initial_check,
        {ROUTE_NO_DATA: END, ROUTE_CONTINUE: NODE_EXPLORATORY_SEARCH},
    )

    # exploratory_search (conditional)
    g.add_conditional_edges(
        NODE_EXPLORATORY_SEARCH,
        _route_exploratory_search,
        {ROUTE_NO_DATA: END, ROUTE_CONTINUE: NODE_QUARTER_INFO},
    )

    # Phase 1: linear chain
    g.add_edge(NODE_QUARTER_INFO, NODE_CONCEPT_EXTRACTION)
    g.add_edge(NODE_CONCEPT_EXTRACTION, NODE_CONCEPT_SEARCH)
    g.add_edge(NODE_CONCEPT_SEARCH, NODE_CONCEPT_SEARCH_POSTPROCESSING)

    # concept_search_postprocessing (conditional)
    g.add_conditional_edges(
        NODE_CONCEPT_SEARCH_POSTPROCESSING,
        _route_concept_postprocessing,
        {ROUTE_NO_DATA: END, ROUTE_CONTINUE: SUBGRAPH_BULLET_GENERATION},
    )

    # After bullet subgraph (conditional)
    g.add_conditional_edges(
        SUBGRAPH_BULLET_GENERATION,
        _route_after_bullet_subgraph,
        {ROUTE_NO_DATA: END, ROUTE_CONTINUE: NODE_ENTITY_GROUNDING_CHECK},
    )

    # Grounding → Novelty Embedding → persist (rewrite + relevance check steps removed)
    g.add_edge(NODE_ENTITY_GROUNDING_CHECK, NODE_EMBED_AND_RETRIEVE)
    g.add_edge(NODE_EMBED_AND_RETRIEVE, NODE_NOVELTY_JUDGMENT_EMBEDDING)
    g.add_edge(NODE_NOVELTY_JUDGMENT_EMBEDDING, NODE_PERSIST_NOVEL_EMBEDDINGS)

    # Novelty Search: linear chain (4 nodes)
    g.add_edge(NODE_PERSIST_NOVEL_EMBEDDINGS, NODE_NOVELTY_SEARCH_PARSE_AND_PLAN)
    g.add_edge(NODE_NOVELTY_SEARCH_PARSE_AND_PLAN, NODE_NOVELTY_SEARCH_FETCH)
    g.add_edge(NODE_NOVELTY_SEARCH_FETCH, NODE_NOVELTY_SEARCH_JUDGMENT)
    g.add_edge(NODE_NOVELTY_SEARCH_JUDGMENT, NODE_NOVELTY_SEARCH_REWRITE)
    g.add_edge(NODE_NOVELTY_SEARCH_REWRITE, NODE_RELEVANCE_SCORE_SEARCH)
    g.add_edge(NODE_RELEVANCE_SCORE_SEARCH, NODE_SAVE_NOVEL_BULLETS)

    # save_novel_bullets (conditional: no_data / post_processing / build_report)
    g.add_conditional_edges(
        NODE_SAVE_NOVEL_BULLETS,
        _route_save_novel_bullets,
        {
            ROUTE_NO_DATA: END,
            ROUTE_POST_PROCESSING: NODE_REDUNDANCY_CHECK,
            ROUTE_BUILD_REPORT: NODE_BUILD_REPORT,
        },
    )

    # Post-processing: linear chain → build_report
    g.add_edge(NODE_REDUNDANCY_CHECK, NODE_THEMATIC_CONSOLIDATION)
    g.add_edge(NODE_THEMATIC_CONSOLIDATION, NODE_STANDALONE_VALIDATION)
    g.add_edge(NODE_STANDALONE_VALIDATION, NODE_BUILD_REPORT)

    # build_report → END
    g.add_edge(NODE_BUILD_REPORT, END)

    return g


def compile_brief_graph():
    """Return a compiled, executable Brief pipeline graph."""
    return build_brief_graph().compile()

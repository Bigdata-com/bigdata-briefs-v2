"""
Subgraph: Bullets_Generation_and_Scoring

A nested LangGraph StateGraph that loops over themes sequentially:

    START → generate_theme_bullets → score_bullet_relevance
      ─[next_theme]→ generate_theme_bullets  (loop back)
      ─[done]──────→ END                     (exit subgraph)

Each iteration advances ``active_theme_index`` by 1 (done inside
``score_bullet_relevance``). The loop terminates when the incremented index
equals ``len(themes)``.

The subgraph uses the same ``BriefGraphState`` schema as the parent graph,
so state is passed through without mapping.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from bigdata_briefs.graph.constants import (
    NODE_BULLETS_GENERATION,
    NODE_RELEVANCE_SCORE,
    ROUTE_DONE,
    ROUTE_NEXT_THEME,
)
from bigdata_briefs.graph.node_logger import with_node_log
from bigdata_briefs.graph.nodes.phase2_bullets.generate_theme_bullets import (
    produce_bullets_for_theme,
)
from bigdata_briefs.graph.nodes.phase2_bullets.score_bullet_relevance import (
    score_and_gate_bullet_relevance,
)
from bigdata_briefs.graph.state import BriefGraphState


# ── Conditional edge ──────────────────────────────────────────────────────────


def _route_theme_loop(state: BriefGraphState) -> str:
    """
    Continue looping when there are still themes to process, exit otherwise.

    ``score_bullet_relevance`` increments ``active_theme_index`` before this
    edge is evaluated, so the check is: incremented index < len(themes).
    """
    next_index: int = state.get("active_theme_index", 0)
    themes: list[str] = state.get("themes", [])
    if next_index < len(themes):
        return ROUTE_NEXT_THEME
    return ROUTE_DONE


# ── Subgraph builder ──────────────────────────────────────────────────────────


def build_bullet_generation_subgraph() -> StateGraph:
    """
    Build and return the compiled ``Bullets_Generation_and_Scoring`` subgraph.

    Call ``.compile()`` on the returned object to get an executable graph.
    """
    sg = StateGraph(BriefGraphState)

    L = with_node_log
    sg.add_node(NODE_BULLETS_GENERATION, L(NODE_BULLETS_GENERATION, produce_bullets_for_theme))
    sg.add_node(NODE_RELEVANCE_SCORE, L(NODE_RELEVANCE_SCORE, score_and_gate_bullet_relevance))

    sg.add_edge(START, NODE_BULLETS_GENERATION)
    sg.add_edge(NODE_BULLETS_GENERATION, NODE_RELEVANCE_SCORE)

    sg.add_conditional_edges(
        NODE_RELEVANCE_SCORE,
        _route_theme_loop,
        {
            ROUTE_NEXT_THEME: NODE_BULLETS_GENERATION,
            ROUTE_DONE: END,
        },
    )

    return sg


def compile_bullet_generation_subgraph():
    """Return a compiled, executable bullet generation subgraph."""
    return build_bullet_generation_subgraph().compile()

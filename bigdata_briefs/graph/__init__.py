"""
Brief 2.0 LangGraph Pipeline.

Entry points:
    from bigdata_briefs.graph import build_brief_graph, BriefGraphState, RuntimeDependencies
"""

from bigdata_briefs.graph.state import BriefGraphState, BulletPointRecord
from bigdata_briefs.graph.dependencies import RuntimeDependencies

__all__ = [
    "BriefGraphState",
    "BulletPointRecord",
    "RuntimeDependencies",
]

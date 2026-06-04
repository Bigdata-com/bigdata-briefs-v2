"""Locate and import from the parent sentiment_tool module."""

from __future__ import annotations

import os
import sys


def _find_sentiment_tool_dir() -> str:
    """Return the directory containing sentiment_tool.py.

    Checks SENTIMENT_TOOL_DIR env var first, then walks up from this file's
    install location until sentiment_tool.py is found.
    """
    env_path = os.environ.get("SENTIMENT_TOOL_DIR")
    if env_path and os.path.isfile(os.path.join(env_path, "sentiment_tool.py")):
        return env_path

    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(12):
        if os.path.isfile(os.path.join(current, "sentiment_tool.py")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    raise RuntimeError(
        "Cannot locate sentiment_tool.py. "
        "Set SENTIMENT_TOOL_DIR=/path/to/sentiment_tool env var."
    )


def ensure_sentiment_tool_on_path() -> None:
    """Add the sentiment_tool directory to sys.path if not already importable."""
    try:
        import sentiment_tool  # noqa: F401
        return
    except ImportError:
        pass
    tool_dir = _find_sentiment_tool_dir()
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)

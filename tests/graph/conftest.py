"""
Shared fixtures and helpers for all graph node tests.

Conventions:
  - BASE_STATE: minimal BriefGraphState dict for any node
  - make_bullet(): creates a BulletPointRecord serialised as dict
  - make_deps(): RuntimeDependencies with mock services
  - make_config(): wraps deps in a LangGraph RunnableConfig dict
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlmodel import create_engine

from bigdata_briefs.graph.dependencies import RuntimeDependencies
from bigdata_briefs.graph.state import BulletPointRecord, record_to_bullet


# ── Minimal state ─────────────────────────────────────────────────────────────

BASE_STATE: dict = {
    "entity_id": "ENTITY123",
    "entity_name": "Test Corp",
    "entity_type": "company",
    "entity_ticker": "TC",
    "report_start_date": "2025-01-01",
    "report_end_date": "2025-01-31",
    "request_id": "req-001",
    "config": {},
    "current_quarter_title": "Q1 2025",
    "active_theme_index": 0,
    "themes": ["Theme A"],
    "bullet_points": [],
    "node_metrics": [],
    "source_references": {},
    "extracted_concepts": {"categories": []},
    "raw_concept_results": {},
    "processed_concept_results": {},
}


# ── Bullet factory ────────────────────────────────────────────────────────────

def make_bullet(
    text: str = "Revenue grew 15% year-over-year.",
    theme: str = "Theme A",
    is_active: bool = True,
    citations: list[str] | None = None,
    **kwargs,
) -> dict:
    """Create a serialised BulletPointRecord dict for use in test states."""
    record = BulletPointRecord(
        trace_id=str(uuid4()),
        theme=theme,
        text=text,
        is_active=is_active,
        citations=citations if citations is not None else ["CQS:REF0"],
        **kwargs,
    )
    return record_to_bullet(record)


# ── Deps / config factories ───────────────────────────────────────────────────

def make_deps(engine=None) -> RuntimeDependencies:
    """Build RuntimeDependencies with mock services and an optional real engine."""
    if engine is None:
        engine = create_engine("sqlite:///:memory:")
    return RuntimeDependencies(
        engine=engine,
        query_service=MagicMock(),
        llm_client=MagicMock(),
        brief_service=MagicMock(),
        novelty_service=MagicMock(),
        embedding_client=MagicMock(),
        embedding_storage=MagicMock(),
        generated_bullet_storage=MagicMock(),
    )


def make_config(deps: RuntimeDependencies | None = None) -> dict:
    """Wrap deps in a LangGraph-style RunnableConfig dict."""
    return {"configurable": {"deps": deps if deps is not None else make_deps()}}


# ── Shared pytest fixtures ────────────────────────────────────────────────────

@pytest.fixture
def base_state() -> dict:
    return dict(BASE_STATE)


@pytest.fixture
def deps() -> RuntimeDependencies:
    return make_deps()


@pytest.fixture
def config(deps) -> dict:
    return make_config(deps)

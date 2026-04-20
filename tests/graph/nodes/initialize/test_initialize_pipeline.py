"""
Tests for the initialize_pipeline node.

Conventions established here for all graph node tests:
  - Build a RunnableConfig as ``{"configurable": {"deps": RuntimeDependencies(...)}}``
  - Services not used by the node under test are replaced with ``MagicMock()``
  - The engine is always a real in-memory SQLite so we verify actual DDL behaviour
  - State dict can be empty (``{}``) when the node does not read from it
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from bigdata_briefs.graph.constants import NODE_INITIALIZE_PIPELINE, SERVICE_TYPE_NONE
from bigdata_briefs.graph.dependencies import RuntimeDependencies
from bigdata_briefs.graph.nodes.initialize.initialize_pipeline import initialize_pipeline


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_deps(engine) -> RuntimeDependencies:
    """Build a minimal RuntimeDependencies with a real engine and mock services."""
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


def _make_config(engine) -> dict:
    """Build a LangGraph-style RunnableConfig with injected deps."""
    return {"configurable": {"deps": _make_deps(engine)}}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_engine():
    """In-memory SQLite engine with no tables created yet."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def seeded_engine():
    """In-memory SQLite engine that already has the full schema."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestInitializePipelineCreatesSchema:
    def test_tables_created_on_first_run(self, fresh_engine):
        """All expected tables must exist after the node runs against a blank DB."""
        result = initialize_pipeline({}, _make_config(fresh_engine))

        inspector = inspect(fresh_engine)
        tables = set(inspector.get_table_names())
        assert "sqlbulletpointembedding" in tables
        assert "sqlchunktexthash" in tables
        assert "sqlentityorchestrationstate" in tables
        assert "sqlentitypipelinerunlog" in tables

    def test_report_window_columns_added(self, fresh_engine):
        """report_window_start / report_window_end must exist after init."""
        initialize_pipeline({}, _make_config(fresh_engine))

        with fresh_engine.connect() as conn:
            rows = conn.execute(
                text("PRAGMA table_info(sqlbulletpointembedding)")
            ).fetchall()
        col_names = {row[1] for row in rows}
        assert "report_window_start" in col_names
        assert "report_window_end" in col_names


class TestInitializePipelineIdempotency:
    def test_second_call_does_not_raise(self, fresh_engine):
        """Calling the node twice on the same DB must not raise."""
        config = _make_config(fresh_engine)
        initialize_pipeline({}, config)
        initialize_pipeline({}, config)  # must not raise

    def test_tables_already_exist_does_not_raise(self, seeded_engine):
        """Running against a DB that already has the full schema must not raise."""
        initialize_pipeline({}, _make_config(seeded_engine))


class TestInitializePipelineReturnValue:
    def test_returns_node_metrics_list(self, fresh_engine):
        """Node must return a dict with a ``node_metrics`` key containing one record."""
        result = initialize_pipeline({}, _make_config(fresh_engine))

        assert "node_metrics" in result
        assert len(result["node_metrics"]) == 1

    def test_metrics_record_shape(self, fresh_engine):
        """The NodeMetricsRecord dict must have the expected fields and values."""
        result = initialize_pipeline({}, _make_config(fresh_engine))

        record = result["node_metrics"][0]
        assert record["node_id"] == NODE_INITIALIZE_PIPELINE
        assert record["service_type"] == SERVICE_TYPE_NONE
        assert record["wall_time_ms"] >= 0
        assert record["extra"]["schema_ensured"] is True

    def test_no_other_state_keys_returned(self, fresh_engine):
        """The node must not write any state key other than node_metrics."""
        result = initialize_pipeline({}, _make_config(fresh_engine))

        assert set(result.keys()) == {"node_metrics"}


class TestInitializePipelineFailurePropagation:
    def test_schema_error_propagates(self, fresh_engine):
        """If ensure_orchestration_schema raises, the exception must not be swallowed."""
        config = _make_config(fresh_engine)

        with patch(
            "bigdata_briefs.graph.nodes.initialize.initialize_pipeline.ensure_orchestration_schema",
            side_effect=RuntimeError("DB unreachable"),
        ):
            with pytest.raises(RuntimeError, match="DB unreachable"):
                initialize_pipeline({}, config)

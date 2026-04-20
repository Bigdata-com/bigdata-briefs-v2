"""
Node: initialize_pipeline

First node in the graph. Ensures all infrastructure required by the
pipeline exists before any real work starts.

Specifically:
  - Calls ``ensure_orchestration_schema(engine)`` which creates all DB
    tables if they are missing (idempotent — safe to call on every run).

On the very first run the tables are created from scratch.
On every subsequent run the call is a fast no-op.

If setup fails the exception propagates immediately, before any LLM or
search calls are made.

Service type: none (DB DDL only)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig

from bigdata_briefs.graph.constants import NODE_INITIALIZE_PIPELINE, SERVICE_TYPE_NONE
from bigdata_briefs.graph.dependencies import get_deps
from bigdata_briefs.graph.state import BriefGraphState, NodeMetricsRecord
from bigdata_briefs.orchestration.db import ensure_orchestration_schema


def initialize_pipeline(state: BriefGraphState, config: RunnableConfig) -> dict:
    """
    LangGraph node — initialize_pipeline.

    Runs DB schema creation (idempotent) so the pipeline can always be
    invoked against a fresh environment without any prior manual setup.
    """
    deps = get_deps(config)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    ensure_orchestration_schema(deps.engine)

    wall_ms = (time.monotonic() - t0) * 1000
    metrics = NodeMetricsRecord(
        node_id=NODE_INITIALIZE_PIPELINE,
        service_type=SERVICE_TYPE_NONE,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        wall_time_ms=wall_ms,
        extra={"schema_ensured": True},
    )

    return {"node_metrics": [metrics.model_dump()]}

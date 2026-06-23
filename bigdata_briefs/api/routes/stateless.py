"""
Routes: stateless (database-less) operations

    POST /api/v1/stateless/briefs       → fan out many entities, return a job_id
    GET  /api/v1/stateless/jobs/{id}     → poll a fan-out job (in-memory, no DB)

These never touch SQLite. Novelty is search-only. Concurrency (and therefore peak
memory) is bounded by the shared ``entity_executor`` (MAX_CONCURRENT_ENTITIES), not by
batch size. The job registry is an in-process dict; finished entity reports are evicted
when a job is read to completion. A single entity is just a batch of length 1.
"""

from __future__ import annotations

import uuid
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request

from bigdata_briefs import logger
from bigdata_briefs.api.auth import require_api_key
from bigdata_briefs.api.dependencies import (
    get_connection_sem,
    get_entity_executor,
    get_http_client,
    get_rate_limiter,
)
from bigdata_briefs.api.schemas import (
    StatelessBriefsRequest,
    StatelessJobAccepted,
    StatelessJobStatus,
)
from bigdata_briefs.orchestration.config_load import (
    load_pipeline_config_dict,
    resolve_config_path,
)
from bigdata_briefs.orchestration.stateless_runner import run_entity_stateless

router = APIRouter(tags=["stateless"], dependencies=[Depends(require_api_key)])


def _pipeline_config(categories: list[str] | None) -> dict:
    cfg = load_pipeline_config_dict(resolve_config_path(None))
    if categories:
        cfg["categories"] = categories
    return cfg


@router.post("/stateless/briefs", response_model=StatelessJobAccepted, status_code=202)
def stateless_briefs(
    body: StatelessBriefsRequest,
    request: Request,
    executor=Depends(get_entity_executor),
    rate_limiter=Depends(get_rate_limiter),
    connection_sem=Depends(get_connection_sem),
    http_client=Depends(get_http_client),
) -> StatelessJobAccepted:
    """Fan out across the bounded executor; collect results into the job registry."""
    if not body.entity_ids:
        raise HTTPException(status_code=422, detail="No entity_ids to run.")
    if body.window_end <= body.window_start:
        raise HTTPException(status_code=422, detail="window end must be after start")

    job_id = str(uuid.uuid4())
    registry = request.app.state.job_registry
    cfg = _pipeline_config(body.categories)
    entry = {
        "status": "running",
        "total": len(body.entity_ids),
        "done": 0,
        "results": {},
        "errors": {},
        "progress": {eid: "queued" for eid in body.entity_ids},
        "lock": Lock(),
    }
    registry[job_id] = entry

    def _one(eid: str):
        def _on_phase(phase: str):
            with entry["lock"]:
                entry["progress"][eid] = phase

        try:
            rep = run_entity_stateless(
                entity_id=eid,
                window_start=body.window_start,
                window_end=body.window_end,
                pipeline_config=cfg,
                rate_limiter=rate_limiter,
                connection_sem=connection_sem,
                http_client=http_client,
                progress_cb=_on_phase,
            )
            with entry["lock"]:
                entry["results"][eid] = rep
                entry["progress"][eid] = "done"
        except Exception as e:  # noqa: BLE001 — per-entity isolation
            logger.exception("stateless batch entity failed: %s", eid)
            with entry["lock"]:
                entry["errors"][eid] = str(e)
                entry["progress"][eid] = "failed"
        finally:
            with entry["lock"]:
                entry["done"] += 1
                if entry["done"] >= entry["total"]:
                    entry["status"] = "finished"

    for eid in body.entity_ids:
        executor.submit(_one, eid)

    return StatelessJobAccepted(job_id=job_id, total=len(body.entity_ids))


@router.get("/stateless/jobs/{job_id}", response_model=StatelessJobStatus)
def stateless_job_status(job_id: str, request: Request) -> StatelessJobStatus:
    """Return job progress and collected reports. Evicts the job once fully read."""
    registry = request.app.state.job_registry
    entry = registry.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    with entry["lock"]:
        status = entry["status"]
        resp = StatelessJobStatus(
            job_id=job_id,
            status=status,
            total=entry["total"],
            done=entry["done"],
            progress=dict(entry["progress"]),
            results=dict(entry["results"]),
            errors=dict(entry["errors"]),
        )
        # Drain-on-read: once finished and reported, free the memory.
        if status == "finished":
            registry.pop(job_id, None)
    return resp

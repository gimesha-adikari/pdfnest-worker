from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.jobs.actors import (
    test_job,
    editor_compile_job,
    editor_extract_job,
)
from app.jobs.models import JobQueue, JobRecord, TestJobRequest
from app.jobs.store import create_job, get_job, list_jobs, request_cancel

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["jobs"],
)

QUEUE_TO_ACTOR = {
    JobQueue.default: test_job,
    JobQueue.office: editor_compile_job,
    JobQueue.render: editor_extract_job,
}


@router.post("/test", response_model=JobRecord)
def queue_test_job(request: TestJobRequest | None = Body(default=None)) -> JobRecord:
    payload = request.payload if request else {}
    queue_name = request.queue_name if request else JobQueue.default
    steps = request.steps if request else 10
    duration_seconds = request.duration_seconds if request else 5.0

    actor = QUEUE_TO_ACTOR.get(queue_name)
    if actor is None:
        raise HTTPException(status_code=400, detail=f"Unsupported queue: {queue_name}")

    job = create_job("test", queue_name=queue_name, payload=payload)
    actor.send(job.id, payload, steps, duration_seconds)
    return job


@router.get("", response_model=list[JobRecord])
def read_jobs(limit: int = 50) -> list[JobRecord]:
    return list_jobs(limit=limit)


@router.get("/{job_id}", response_model=JobRecord)
def read_job(job_id: str) -> JobRecord:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobRecord)
def cancel_job(job_id: str) -> JobRecord:
    job = request_cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
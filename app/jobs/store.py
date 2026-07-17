from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.core.redis import redis_client
from app.jobs.models import JobQueue, JobRecord, JobState

JOB_KEY_PREFIX = "pdfnest:jobs:"
JOB_INDEX_KEY = "pdfnest:jobs:index"
TERMINAL_STATES = {
    JobState.succeeded,
    JobState.failed,
    JobState.cancelled,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_key(job_id: str) -> str:
    return f"{JOB_KEY_PREFIX}{job_id}"


def create_job(
    job_type: str,
    *,
    queue_name: JobQueue = JobQueue.default,
    payload: dict[str, Any] | None = None,
) -> JobRecord:
    now = utcnow()
    job = JobRecord(
        id=str(uuid4()),
        job_type=job_type,
        queue_name=queue_name,
        created_at=now,
        updated_at=now,
        payload=payload or {},
    )
    save_job(job)
    return job


def save_job(job: JobRecord) -> None:
    job.updated_at = utcnow()
    redis_client.set(
        job_key(job.id),
        job.model_dump_json(),
        ex=settings.job_ttl_seconds,
    )
    redis_client.zadd(JOB_INDEX_KEY, {job.id: job.created_at.timestamp()})


def get_job(job_id: str) -> JobRecord | None:
    raw = redis_client.get(job_key(job_id))
    if not raw:
        return None
    return JobRecord.model_validate_json(raw)


def list_jobs(limit: int = 50) -> list[JobRecord]:
    if limit <= 0:
        return []

    job_ids = redis_client.zrevrange(JOB_INDEX_KEY, 0, limit - 1)
    jobs: list[JobRecord] = []

    for job_id in job_ids:
        job = get_job(job_id)
        if job is not None:
            jobs.append(job)

    return jobs


def update_job(
    job_id: str,
    *,
    status: JobState | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    progress: int | None = None,
    message: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    cancel_requested: bool | None = None,
) -> JobRecord | None:
    job = get_job(job_id)
    if job is None:
        return None

    if status is not None:
        job.status = status
    if started_at is not None:
        job.started_at = started_at
    if finished_at is not None:
        job.finished_at = finished_at
    if progress is not None:
        job.progress = max(0, min(100, progress))
    if message is not None:
        job.message = message
    if result is not None:
        job.result = result
    if error is not None:
        job.error = error
    if cancel_requested is not None:
        job.cancel_requested = cancel_requested

    save_job(job)
    return job


def request_cancel(job_id: str) -> JobRecord | None:
    job = get_job(job_id)
    if job is None:
        return None

    if job.status in TERMINAL_STATES:
        return job

    job.cancel_requested = True
    if job.status == JobState.queued:
        job.status = JobState.cancel_requested
        job.message = "Cancellation requested"
    else:
        job.message = "Cancellation requested"

    save_job(job)
    return job
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from redis.exceptions import WatchError

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


def prune_expired_job_index() -> None:
    """
    Removes job IDs from pdfnest:jobs:index created older than JOB_TTL_SECONDS (24h).
    Executed opportunistically during save_job and list_jobs to prevent memory growth.
    """
    try:
        cutoff = utcnow().timestamp() - settings.job_ttl_seconds
        redis_client.zremrangebyscore(JOB_INDEX_KEY, "-inf", cutoff)
    except Exception:
        pass


def save_job(job: JobRecord) -> None:
    job.updated_at = utcnow()
    key = job_key(job.id)
    ttl = settings.job_ttl_seconds

    with redis_client.pipeline() as pipe:
        while True:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if raw:
                    existing = JobRecord.model_validate_json(raw)
                    # Terminal states are immutable
                    if existing.status in TERMINAL_STATES:
                        if existing.status != job.status:
                            pipe.unwatch()
                            return
                    # Monotonic progress check for running -> running
                    if existing.status == JobState.running and job.status == JobState.running:
                        if job.progress < existing.progress:
                            job.progress = existing.progress

                pipe.multi()
                pipe.set(key, job.model_dump_json(), ex=ttl)
                pipe.zadd(JOB_INDEX_KEY, {job.id: job.created_at.timestamp()})
                pipe.execute()
                break
            except WatchError:
                continue

    prune_expired_job_index()


def get_job(job_id: str) -> JobRecord | None:
    key = job_key(job_id)
    raw = redis_client.get(key)
    if not raw:
        return None
    job = JobRecord.model_validate_json(raw)

    if job.status == JobState.running and job.status not in TERMINAL_STATES:
        elapsed = (utcnow() - job.updated_at).total_seconds()
        if elapsed > settings.stuck_job_timeout_seconds:
            with redis_client.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(key)
                        current_raw = pipe.get(key)
                        if not current_raw:
                            pipe.unwatch()
                            return None
                        current_job = JobRecord.model_validate_json(current_raw)
                        if current_job.status in TERMINAL_STATES:
                            pipe.unwatch()
                            return current_job

                        current_elapsed = (utcnow() - current_job.updated_at).total_seconds()
                        if current_elapsed > settings.stuck_job_timeout_seconds:
                            current_job.status = JobState.failed
                            current_job.error = "Worker process terminated unexpectedly or job timed out."
                            current_job.finished_at = utcnow()
                            current_job.updated_at = utcnow()

                            pipe.multi()
                            pipe.set(key, current_job.model_dump_json(), ex=settings.job_ttl_seconds)
                            pipe.execute()
                            return current_job
                        else:
                            pipe.unwatch()
                            return current_job
                    except WatchError:
                        continue

    return job


def list_jobs(limit: int = 50) -> list[JobRecord]:
    if limit <= 0:
        return []

    prune_expired_job_index()
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
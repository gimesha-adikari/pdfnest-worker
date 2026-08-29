from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

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
    job_id: str | None = None,
    queue_name: JobQueue = JobQueue.default,
    payload: dict[str, Any] | None = None,
    owner_identity: str | None = None,
) -> JobRecord:
    now = utcnow()
    job = JobRecord(
        id=job_id or str(uuid4()),
        job_type=job_type,
        queue_name=queue_name,
        created_at=now,
        updated_at=now,
        payload=payload or {},
        owner_identity=owner_identity,
    )
    save_job(job)
    return job


def prune_expired_job_index() -> None:
    """Prune expired index entries opportunistically to bound Redis memory."""
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
                    # A late worker update must not overwrite a terminal outcome.
                    if existing.status in TERMINAL_STATES:
                        if existing.status != job.status:
                            pipe.unwatch()
                            return

                    # Cancellation remains sticky until the actor records cancellation.
                    if existing.cancel_requested or existing.status == JobState.cancel_requested:
                        if job.status not in (JobState.cancelled, JobState.cancel_requested):
                            job.cancel_requested = True
                            job.status = JobState.cancel_requested

                    # Concurrent worker updates may arrive out of order.
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

    # Sync task status to Go backend Redis key format pdfnest:tasks:<job.id>
    try:
        task_key = f"pdfnest:tasks:{job.id}"
        existing_task_raw = redis_client.get(task_key)
        existing_task = json.loads(existing_task_raw) if existing_task_raw else {}

        status_map = {
            JobState.queued: "QUEUED",
            JobState.running: "PROCESSING",
            JobState.succeeded: "COMPLETED",
            JobState.failed: "FAILED",
            JobState.cancelled: "CANCELLED",
            JobState.cancel_requested: "CANCELLED",
        }

        task_status = status_map.get(job.status, "PROCESSING")
        result_key = (job.result or {}).get("artifact_key", "")

        task_data = {
            "id": job.id,
            "status": task_status,
            "progress": job.progress,
            "resultKey": result_key,
            "resultUrl": f"r2://{result_key}" if result_key else "",
            "ownerIdentity": existing_task.get("ownerIdentity", job.owner_identity or ""),
            "reservationId": existing_task.get("reservationId", ""),
            "downloadToken": existing_task.get("downloadToken", ""),
            "error": job.error or job.message or "",
            "updatedAt": int(job.updated_at.timestamp()),
        }

        redis_client.set(task_key, json.dumps(task_data), ex=3600)
    except Exception as e:
        logger.warning(f"[TASK SYNC FAIL] Failed to sync task {job.id}: {e}")


def get_job(job_id: str) -> JobRecord | None:
    key = redis_client.get(job_key(job_id))
    if not key:
        return None
    job = JobRecord.model_validate_json(key)

    if job.status == JobState.running and job.status not in TERMINAL_STATES:
        elapsed = (utcnow() - job.updated_at).total_seconds()
        if elapsed > settings.stuck_job_timeout_seconds:
            with redis_client.pipeline() as pipe:
                while True:
                    try:
                        k = job_key(job_id)
                        pipe.watch(k)
                        current_raw = pipe.get(k)
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
                            pipe.set(k, current_job.model_dump_json(), ex=settings.job_ttl_seconds)
                            pipe.execute()
                            return current_job
                        else:
                            pipe.unwatch()
                            return current_job
                    except WatchError:
                        continue

    return job


def claim_job(job_id: str) -> JobRecord | None:
    """Atomically claim a queued job so duplicate delivery cannot re-run OCR."""
    key = job_key(job_id)
    while True:
        try:
            with redis_client.pipeline() as pipe:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    pipe.unwatch()
                    return None
                job = JobRecord.model_validate_json(raw)
                if job.status != JobState.queued and job.status != JobState.cancel_requested:
                    pipe.unwatch()
                    return None
                if job.cancel_requested or job.status == JobState.cancel_requested:
                    pipe.unwatch()
                    return None
                job.status = JobState.running
                job.started_at = utcnow()
                job.updated_at = utcnow()
                pipe.multi()
                pipe.set(key, job.model_dump_json(), ex=settings.job_ttl_seconds)
                pipe.zadd(JOB_INDEX_KEY, {job.id: job.created_at.timestamp()})
                pipe.execute()
                break
        except WatchError:
            continue
    # Keep the existing Go task mirror and retention bookkeeping in sync.
    save_job(job)
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
    error_code: str | None = None,
    cancel_requested: bool | None = None,
    total_pages: int | None = None,
    completed_pages: int | None = None,
    failed_pages: list[int] | None = None,
    current_page: int | None = None,
    page_statuses: dict[str, str] | None = None,
    warnings: list[str] | None = None,
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
    if error_code is not None:
        job.error_code = error_code
    if cancel_requested is not None:
        job.cancel_requested = cancel_requested
    if total_pages is not None:
        job.total_pages = max(0, total_pages)
    if completed_pages is not None:
        job.completed_pages = max(0, completed_pages)
    if failed_pages is not None:
        job.failed_pages = list(failed_pages)
    if current_page is not None:
        job.current_page = current_page
    if page_statuses is not None:
        job.page_statuses = dict(page_statuses)
    if warnings is not None:
        job.warnings = list(warnings)

    save_job(job)
    return job


def request_cancel(job_id: str, owner_identity: str | None = None) -> JobRecord | None:
    job = get_job(job_id)
    if job is None:
        return None

    if owner_identity and owner_identity.strip():
        job_owner = (job.payload or {}).get("ownerIdentity") or ""
        if job_owner and job_owner.strip() and job_owner.strip() != owner_identity.strip():
            raise PermissionError("You are not authorized to cancel this job")

    if job.status in TERMINAL_STATES:
        return job

    job.cancel_requested = True
    job.status = JobState.cancel_requested
    job.message = "Cancellation requested"

    save_job(job)
    return job

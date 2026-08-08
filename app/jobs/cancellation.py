from __future__ import annotations

import logging
from app.jobs.models import JobState
from app.jobs.store import get_job

logger = logging.getLogger(__name__)


class JobCancelledException(Exception):
    """Raised when a Python worker job is cooperatively cancelled by user."""
    pass


def check_cancellation(job_id: str) -> None:
    if not job_id:
        return
    job = get_job(job_id)
    if job is None:
        return
    if job.cancel_requested or job.status in (JobState.cancelled, JobState.cancel_requested):
        logger.info("Cooperative cancellation triggered for job %s", job_id)
        raise JobCancelledException(f"Job {job_id} cancelled by user")

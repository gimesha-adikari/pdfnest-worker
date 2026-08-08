from __future__ import annotations

import pytest
from app.jobs.cancellation import JobCancelledException, check_cancellation
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job, request_cancel, save_job


def test_request_cancel_transitions_status() -> None:
    job = create_job("test", queue_name=JobQueue.default, payload={"ownerIdentity": "user:alice"})
    assert job.status == JobState.queued

    updated = request_cancel(job.id, owner_identity="user:alice")
    assert updated is not None
    assert updated.cancel_requested is True
    assert updated.status == JobState.cancel_requested


def test_request_cancel_unauthorized_raises_permission_error() -> None:
    job = create_job("test", queue_name=JobQueue.default, payload={"ownerIdentity": "user:alice"})

    with pytest.raises(PermissionError):
        request_cancel(job.id, owner_identity="user:bob")


def test_save_job_protects_cancel_requested_state() -> None:
    job = create_job("test", queue_name=JobQueue.default, payload={"ownerIdentity": "user:alice"})
    request_cancel(job.id, owner_identity="user:alice")

    # Attempt to overwrite cancel_requested with running status
    job_read = get_job(job.id)
    assert job_read is not None
    assert job_read.status == JobState.cancel_requested

    job_read.status = JobState.running
    save_job(job_read)

    job_after = get_job(job.id)
    assert job_after is not None
    assert job_after.status == JobState.cancel_requested
    assert job_after.cancel_requested is True


def test_check_cancellation_raises_exception() -> None:
    job = create_job("test", queue_name=JobQueue.default, payload={"ownerIdentity": "user:alice"})
    request_cancel(job.id, owner_identity="user:alice")

    with pytest.raises(JobCancelledException):
        check_cancellation(job.id)

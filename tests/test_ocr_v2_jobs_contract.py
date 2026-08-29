from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.ocr_v2.jobs import _status, _validate_job_id, _validate_source_key
from app.jobs.models import JobState


def test_async_job_boundary_accepts_only_server_style_references() -> None:
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    assert _validate_job_id(job_id) == job_id
    assert _validate_source_key("jobs/ocr_v2/input/document.pdf") == "jobs/ocr_v2/input/document.pdf"
    with pytest.raises(HTTPException):
        _validate_job_id("../../etc/passwd")
    with pytest.raises(HTTPException):
        _validate_source_key("/tmp/document.pdf")
    with pytest.raises(HTTPException):
        _validate_source_key("jobs/../other/document.pdf")


def test_async_status_projection_contains_page_progress_but_not_payload() -> None:
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id="123e4567-e89b-12d3-a456-426614174000",
        status=JobState.running,
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=None,
        progress=50,
        total_pages=4,
        completed_pages=2,
        failed_pages=[1],
        current_page=2,
        page_statuses={"0": "SUCCESS", "1": "FAILED", "2": "RUNNING"},
        warnings=["ENGINE_FALLBACK:PP_OCR_UNAVAILABLE_TO_TESSERACT"],
        result={"artifact_key": "jobs/ocr_v2/results/private.json"},
        owner_identity="user:alice",
        error_code=None,
        error=None,
        payload={"profile": "OCR_TEXT_V2", "language": "eng", "routing_policy": "AUTO", "ownerIdentity": "user:alice"},
    )
    projected = _status(job)
    assert projected.status == "running"
    assert projected.completed_pages == 2
    assert projected.page_statuses["1"] == "FAILED"
    assert projected.result_key == "jobs/ocr_v2/results/private.json"
    assert projected.owner_identity == "user:alice"

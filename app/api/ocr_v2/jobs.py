"""Private authenticated bridge from the backend product API to Dramatiq."""

from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, HTTPException

from app.core.storage import download_to_path
from app.jobs.actors import ocr_v2_job
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job, request_cancel, update_job

from .schemas import OCRV2JobCancelRequest, OCRV2JobStatusResponse, OCRV2JobSubmitRequest


router = APIRouter(prefix="/internal/ocr/v2/jobs", tags=["ocr-v2-internal"])
_JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _validate_source_key(source_key: str) -> str:
    value = source_key.strip()
    if not value or value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Invalid OCR V2 input reference"})
    return value


def _validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id) or job_id.count("-") != 4:
        raise HTTPException(status_code=400, detail={"code": "INVALID_INPUT", "message": "Invalid OCR V2 job identifier"})
    return job_id


def _result_key(job: object) -> str | None:
    result = getattr(job, "result", None) or {}
    key = result.get("artifact_key") if isinstance(result, dict) else None
    return str(key) if key else None


def _status(job: object) -> OCRV2JobStatusResponse:
    payload = job.payload or {}
    return OCRV2JobStatusResponse(
        job_id=job.id,
        status=job.status.value,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        progress=job.progress,
        total_pages=job.total_pages,
        completed_pages=job.completed_pages,
        failed_pages=job.failed_pages,
        current_page=job.current_page,
        page_statuses=job.page_statuses,
        warnings=job.warnings,
        result_key=_result_key(job),
        owner_identity=job.owner_identity or (job.payload or {}).get("ownerIdentity") or "",
        error_code=job.error_code,
        error=job.error if job.status in {JobState.failed, JobState.cancelled} else None,
        profile=str(payload.get("profile", "OCR_TEXT_V2")),
        language=str(payload.get("language", "")),
        routing_policy=str(payload.get("routing_policy", "AUTO")),
    )


@router.post("", response_model=OCRV2JobStatusResponse, status_code=202)
def submit(request: OCRV2JobSubmitRequest) -> OCRV2JobStatusResponse:
    _validate_source_key(request.source_key)
    job = create_job(
        "ocr_text_v2",
        queue_name=JobQueue.ocr,
        payload={
            "request_id": request.request_id,
            "profile": request.profile.value,
            "language": request.language,
            "routing_policy": request.routing_policy.value,
            "source_key": request.source_key,
            "source_name": request.source_name,
            "ownerIdentity": request.owner_identity,
        },
        owner_identity=request.owner_identity,
    )
    if request.total_pages:
        update_job(job.id, total_pages=request.total_pages)
        job = get_job(job.id) or job
    try:
        ocr_v2_job.send(job.id, request.source_key, request.source_name, request.language, request.routing_policy.value)
    except Exception:
        update_job(job.id, status=JobState.failed, error="OCR V2 queue is unavailable.", error_code="TASK_STORAGE_UNAVAILABLE", message="OCR V2 queue submission failed")
        raise HTTPException(status_code=503, detail={"code": "TASK_STORAGE_UNAVAILABLE", "message": "OCR V2 queue is temporarily unavailable"})
    return _status(get_job(job.id) or job)


@router.get("/{job_id}", response_model=OCRV2JobStatusResponse)
def status(job_id: str) -> OCRV2JobStatusResponse:
    job = get_job(_validate_job_id(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "OCR V2 job not found"})
    return _status(job)


@router.get("/{job_id}/result")
def result(job_id: str) -> dict[str, object]:
    job = get_job(_validate_job_id(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "OCR V2 job not found"})
    key = _result_key(job)
    if not key:
        raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY", "message": "OCR V2 result is not ready"})
    path = os.path.join("/tmp", f"pdfnest-ocr-v2-result-{job.id}.json")
    try:
        download_to_path(key, path)
        with open(path, "r", encoding="utf-8") as result_file:
            return json.load(result_file)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail={"code": "RESULT_EXPIRED", "message": "OCR V2 result is no longer available"})
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "RESULT_STORAGE_UNAVAILABLE", "message": "OCR V2 result storage is temporarily unavailable"}) from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/{job_id}/cancel", response_model=OCRV2JobStatusResponse)
def cancel(job_id: str, request: OCRV2JobCancelRequest) -> OCRV2JobStatusResponse:
    job = request_cancel(_validate_job_id(job_id), owner_identity=request.owner_identity)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "OCR V2 job not found"})
    return _status(job)

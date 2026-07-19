from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.storage import build_key, stream_object, upload_fileobj, upload_text
from app.jobs.actors import editor_compile_job, editor_extract_job
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job

from .models import JobSubmissionResponse

router = APIRouter(prefix="/api/v1/editor", tags=["editor"])


@router.post("/extract")
async def extract_layout(
        file: UploadFile = File(...),
        file_password: str | None = Form(default=None),
        source_tracker: str | None = Form(default=None),
):
    source_name = file.filename or "document.pdf"
    source_suffix = Path(source_name).suffix or ".pdf"
    source_key = build_key("jobs/editor/source", suffix=source_suffix)

    try:
        await file.seek(0)
        await run_in_threadpool(
            upload_fileobj,
            file.file,
            source_key,
            content_type=file.content_type or "application/pdf",
        )

        job = create_job(
            "editor_extract",
            queue_name=JobQueue.editor,
            payload={
                "source_key": source_key,
                "has_password": bool(file_password),
                "source_name": source_name,
                "source_tracker": source_tracker or source_name,
            },
        )

        editor_extract_job.send(job.id, source_key, file_password, source_name)

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status.value,
            queue_name=job.queue_name.value,
        )
    except Exception:
        raise


@router.post("/compile", response_model=JobSubmissionResponse)
async def compile_layout(
        file: UploadFile = File(...),
        payload: str = Form(...),
) -> JobSubmissionResponse:
    source_name = file.filename or "document.pdf"
    source_suffix = Path(source_name).suffix or ".pdf"
    source_key = build_key("jobs/editor/source", suffix=source_suffix)
    pages_json_key = build_key("jobs/editor/layout", suffix=".json")

    try:
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        await file.seek(0)
        await run_in_threadpool(
            upload_fileobj,
            file.file,
            source_key,
            content_type=file.content_type or "application/pdf",
        )
        await run_in_threadpool(
            upload_text,
            payload,
            pages_json_key,
            content_type="application/json",
        )

        job = create_job(
            "editor_compile",
            queue_name=JobQueue.editor,
            payload={
                "source_key": source_key,
                "pages_json_key": pages_json_key,
                "source_name": source_name,
            },
        )

        editor_compile_job.send(job.id, source_key, pages_json_key, source_name)

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status.value,
            queue_name=job.queue_name.value,
        )
    except HTTPException:
        raise
    except Exception:
        raise


@router.get("/jobs/{job_id}/download")
async def download_compiled_pdf(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobState.succeeded:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not ready for download. Current status: {job.status.value}",
        )

    result = job.result or {}
    artifact_key = result.get("artifact_key")
    if not artifact_key:
        raise HTTPException(status_code=404, detail="Compiled artifact not found")

    source_name = str((job.payload or {}).get("source_name") or "document.pdf")
    download_name = result.get("artifact_name") or f"edited_{Path(source_name).stem}.pdf"

    stream, content_type = await run_in_threadpool(stream_object, artifact_key)

    return StreamingResponse(
        stream,
        media_type=content_type or "application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"'
        },
    )
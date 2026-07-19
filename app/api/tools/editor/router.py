from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.storage import stream_object
from app.jobs.actors import editor_compile_job, editor_extract_job
from app.jobs.models import JobQueue, JobState, JobSubmissionResponse
from app.jobs.store import create_job, get_job

router = APIRouter(prefix="/api/v1/editor", tags=["editor"])


class ExtractRequest(BaseModel):
    source_key: str
    file_password: str | None = None
    source_name: str | None = None


class CompileRequest(BaseModel):
    source_key: str
    pages_json_key: str
    source_name: str | None = None


@router.post("/extract", response_model=JobSubmissionResponse)
async def extract_layout(payload: ExtractRequest) -> JobSubmissionResponse:
    if not payload.source_key.strip():
        raise HTTPException(status_code=400, detail="source_key is required")

    source_name = payload.source_name or Path(payload.source_key).name or "document.pdf"

    job = create_job(
        "editor_extract",
        queue_name=JobQueue.editor,
        payload=payload.model_dump(exclude_none=True),
    )

    editor_extract_job.send(job.id, payload.source_key, payload.file_password, source_name)

    return JobSubmissionResponse(
        job_id=job.id,
        status=job.status.value,
        queue_name=job.queue_name.value,
    )


@router.post("/compile", response_model=JobSubmissionResponse)
async def compile_layout(payload: CompileRequest) -> JobSubmissionResponse:
    if not payload.source_key.strip():
        raise HTTPException(status_code=400, detail="source_key is required")
    if not payload.pages_json_key.strip():
        raise HTTPException(status_code=400, detail="pages_json_key is required")

    source_name = payload.source_name or Path(payload.source_key).name or "document.pdf"

    job = create_job(
        "editor_compile",
        queue_name=JobQueue.editor,
        payload=payload.model_dump(exclude_none=True),
    )

    editor_compile_job.send(job.id, payload.source_key, payload.pages_json_key, source_name)

    return JobSubmissionResponse(
        job_id=job.id,
        status=job.status.value,
        queue_name=job.queue_name.value,
    )


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
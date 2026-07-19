from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.storage import stream_object
from app.jobs.actors import markup_highlight_job, markup_strikeout_job, markup_underline_job
from app.jobs.models import JobQueue, JobState, JobSubmissionResponse
from app.jobs.store import create_job, get_job

router = APIRouter(prefix="/api/v1/markup", tags=["markup"])


class MarkupSubmitRequest(BaseModel):
    source_key: str
    payload_key: str
    source_name: str | None = None


async def _queue_markup_job(action: str, payload: MarkupSubmitRequest) -> JobSubmissionResponse:
    if not payload.source_key.strip():
        raise HTTPException(status_code=400, detail="source_key is required")
    if not payload.payload_key.strip():
        raise HTTPException(status_code=400, detail="payload_key is required")

    source_name = payload.source_name or Path(payload.source_key).name or "document.pdf"

    job = create_job(
        f"markup_{action}",
        queue_name=JobQueue.markup,
        payload={
            "source_key": payload.source_key,
            "payload_key": payload.payload_key,
            "source_name": source_name,
            "action": action,
        },
    )

    if action == "highlight":
        markup_highlight_job.send(job.id, payload.source_key, payload.payload_key, source_name)
    elif action == "underline":
        markup_underline_job.send(job.id, payload.source_key, payload.payload_key, source_name)
    elif action == "strikeout":
        markup_strikeout_job.send(job.id, payload.source_key, payload.payload_key, source_name)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    return JobSubmissionResponse(
        job_id=job.id,
        status=job.status.value,
        queue_name=job.queue_name.value,
    )


@router.post("/highlight", response_model=JobSubmissionResponse)
async def highlight_layout(payload: MarkupSubmitRequest) -> JobSubmissionResponse:
    return await _queue_markup_job("highlight", payload)


@router.post("/underline", response_model=JobSubmissionResponse)
async def underline_layout(payload: MarkupSubmitRequest) -> JobSubmissionResponse:
    return await _queue_markup_job("underline", payload)


@router.post("/strikeout", response_model=JobSubmissionResponse)
async def strikeout_layout(payload: MarkupSubmitRequest) -> JobSubmissionResponse:
    return await _queue_markup_job("strikeout", payload)


@router.get("/jobs/{job_id}/download")
async def download_markup_pdf(job_id: str):
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
    download_name = result.get("artifact_name") or f"{result.get('action', 'marked')}_{Path(source_name).stem}.pdf"

    stream, content_type = await run_in_threadpool(stream_object, artifact_key)

    return StreamingResponse(
        stream,
        media_type=content_type or "application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"'
        },
    )
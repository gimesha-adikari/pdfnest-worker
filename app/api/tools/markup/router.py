from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.storage import build_key, stream_object, upload_fileobj, upload_text
from app.jobs.actors import markup_highlight_job, markup_strikeout_job, markup_underline_job
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job
from .models import MarkupMode
from app.jobs.models import JobSubmissionResponse

router = APIRouter(prefix="/api/v1/markup", tags=["markup"])


async def _queue_markup_job(
        *,
        action: str,
        file: UploadFile,
        boxes: str,
        file_password: str | None,
        mode: MarkupMode,
) -> JobSubmissionResponse:
    source_name = file.filename or "document.pdf"
    source_suffix = Path(source_name).suffix or ".pdf"

    source_key = build_key("jobs/markup/source", suffix=source_suffix)
    payload_key = build_key(f"jobs/markup/{action}/payload", suffix=".json")

    try:
        try:
            parsed_boxes = json.loads(boxes)
            if not isinstance(parsed_boxes, list):
                raise ValueError("boxes must be a JSON array")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid boxes JSON payload") from exc

        payload = {
            "boxes": parsed_boxes,
            "mode": mode,
            "file_password": file_password,
            "action": action,
        }

        await file.seek(0)
        await run_in_threadpool(
            upload_fileobj,
            file.file,
            source_key,
            content_type=file.content_type or "application/pdf",
        )
        await run_in_threadpool(
            upload_text,
            json.dumps(payload),
            payload_key,
            content_type="application/json",
        )

        job = create_job(
            f"markup_{action}",
            queue_name=JobQueue.markup,
            payload={
                "source_key": source_key,
                "payload_key": payload_key,
                "action": action,
                "source_name": source_name,
            },
        )

        if action == "highlight":
            markup_highlight_job.send(job.id, source_key, payload_key, source_name)
        elif action == "underline":
            markup_underline_job.send(job.id, source_key, payload_key, source_name)
        elif action == "strikeout":
            markup_strikeout_job.send(job.id, source_key, payload_key, source_name)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status.value,
            queue_name=job.queue_name.value,
        )
    except HTTPException:
        raise
    except Exception:
        raise


@router.post("/highlight", response_model=JobSubmissionResponse)
async def highlight_layout(
        file: UploadFile = File(...),
        boxes: str = Form(...),
        file_password: str | None = Form(default=None),
        mode: MarkupMode = Form(default="smart"),
) -> JobSubmissionResponse:
    return await _queue_markup_job(
        action="highlight",
        file=file,
        boxes=boxes,
        file_password=file_password,
        mode=mode,
    )


@router.post("/underline", response_model=JobSubmissionResponse)
async def underline_layout(
        file: UploadFile = File(...),
        boxes: str = Form(...),
        file_password: str | None = Form(default=None),
        mode: MarkupMode = Form(default="smart"),
) -> JobSubmissionResponse:
    return await _queue_markup_job(
        action="underline",
        file=file,
        boxes=boxes,
        file_password=file_password,
        mode=mode,
    )


@router.post("/strikeout", response_model=JobSubmissionResponse)
async def strikeout_layout(
        file: UploadFile = File(...),
        boxes: str = Form(...),
        file_password: str | None = Form(default=None),
        mode: MarkupMode = Form(default="smart"),
) -> JobSubmissionResponse:
    return await _queue_markup_job(
        action="strikeout",
        file=file,
        boxes=boxes,
        file_password=file_password,
        mode=mode,
    )


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
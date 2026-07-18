from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.jobs.actors import markup_highlight_job, markup_strikeout_job, markup_underline_job
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job

from app.api.tools.editor.utils import cleanup_paths, temp_file_path

from .models import MarkupMode
from .models import MarkupJobPayload
from .models import MarkupBox
from .models import MarkupAction
from .models import MarkupJobResult
from app.jobs.models import JobSubmissionResponse

router = APIRouter(prefix="/api/v1/markup", tags=["markup"])


def _queue_markup_job(
        *,
        action: MarkupAction,
        file: UploadFile,
        boxes: str,
        file_password: str | None,
        mode: MarkupMode,
) -> JobSubmissionResponse:
    input_fd, input_path = tempfile.mkstemp(prefix="pdfnest-source-", suffix=".pdf")
    os.close(input_fd)

    payload_path = temp_file_path(prefix=f"pdfnest-{action}-payload-", suffix=".json")

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

        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        job = create_job(
            f"markup_{action}",
            queue_name=JobQueue.markup,
            payload={
                "input_path": input_path,
                "payload_path": payload_path,
                "action": action,
            },
        )

        if action == "highlight":
            markup_highlight_job.send(job.id, input_path, payload_path)
        elif action == "underline":
            markup_underline_job.send(job.id, input_path, payload_path)
        elif action == "strikeout":
            markup_strikeout_job.send(job.id, input_path, payload_path)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status,
            queue_name=job.queue_name,
        )

    except HTTPException:
        cleanup_paths(input_path, payload_path)
        raise
    except Exception:
        cleanup_paths(input_path, payload_path)
        raise


@router.post("/highlight", response_model=JobSubmissionResponse)
async def highlight_layout(
        file: UploadFile = File(...),
        boxes: str = Form(...),
        file_password: str | None = Form(default=None),
        mode: MarkupMode = Form(default="smart"),
) -> JobSubmissionResponse:
    return _queue_markup_job(
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
    return _queue_markup_job(
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
    return _queue_markup_job(
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
    artifact_path = result.get("artifact_path")
    if not artifact_path or not os.path.exists(artifact_path):
        raise HTTPException(status_code=404, detail="Compiled artifact not found")

    action = result.get("action", "marked")
    return FileResponse(
        artifact_path,
        media_type="application/pdf",
        filename=f"{action}_{Path(artifact_path).name}",
        background=BackgroundTask(cleanup_paths, artifact_path),
    )
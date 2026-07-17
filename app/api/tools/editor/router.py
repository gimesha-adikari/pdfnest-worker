from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.jobs.actors import editor_compile_job, editor_extract_job
from app.jobs.models import JobQueue, JobState
from app.jobs.store import create_job, get_job

from .models import JobSubmissionResponse
from .utils import cleanup_paths, temp_file_path

router = APIRouter(prefix="/api/v1/editor", tags=["editor"])


@router.post("/extract")
async def extract_layout(
    file: UploadFile = File(...),
    file_password: str | None = Form(default=None),
    source_tracker: str | None = Form(default=None),
):
    input_fd, input_path = tempfile.mkstemp(prefix="pdfnest-source-", suffix=".pdf")
    os.close(input_fd)

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        job = create_job(
            "editor_extract",
            queue_name=JobQueue.editor,
            payload={
                "input_path": input_path,
                "has_password": bool(file_password),
                "source_tracker": source_tracker or input_path,
            },
        )

        editor_extract_job.send(job.id, input_path, file_password, source_tracker)

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status.value,
            queue_name=job.queue_name.value,
        )
    except Exception:
        cleanup_paths(input_path)
        raise


@router.post("/compile", response_model=JobSubmissionResponse)
async def compile_layout(
    file: UploadFile = File(...),
    payload: str = Form(...),
) -> JobSubmissionResponse:
    input_fd, input_path = tempfile.mkstemp(prefix="pdfnest-source-", suffix=".pdf")
    os.close(input_fd)

    pages_json_path = temp_file_path(prefix="pdfnest-layout-", suffix=".json")

    try:
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        with open(pages_json_path, "w", encoding="utf-8") as f:
            f.write(payload)

        job = create_job(
            "editor_compile",
            queue_name=JobQueue.editor,
            payload={
                "input_path": input_path,
                "pages_json_path": pages_json_path,
            },
        )

        editor_compile_job.send(job.id, input_path, pages_json_path)

        return JobSubmissionResponse(
            job_id=job.id,
            status=job.status.value,
            queue_name=job.queue_name.value,
        )
    except HTTPException:
        cleanup_paths(input_path, pages_json_path)
        raise
    except Exception:
        cleanup_paths(input_path, pages_json_path)
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
    artifact_path = result.get("artifact_path")
    if not artifact_path or not os.path.exists(artifact_path):
        raise HTTPException(status_code=404, detail="Compiled artifact not found")

    return FileResponse(
        artifact_path,
        media_type="application/pdf",
        filename="edited_" + Path(artifact_path).name,
        background=BackgroundTask(cleanup_paths, artifact_path),
    )
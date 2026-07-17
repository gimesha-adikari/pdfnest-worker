from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import dramatiq

from app.api.tools.editor.document import compile_document, extract_document
from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.core.broker import broker  # noqa: F401
from app.jobs.models import JobState
from app.jobs.store import get_job, update_job


@dramatiq.actor(queue_name="default", max_retries=3)
def test_job(job_id: str, payload: dict[str, Any] | None = None) -> None:
    job = get_job(job_id)
    if job is None:
        return

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=10,
        message="Test job started",
    )

    try:
        time.sleep(2)

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "message": "Worker completed successfully",
                "payload": payload or {},
            },
            message="Test job completed",
        )
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Test job failed",
        )
        raise


@dramatiq.actor(queue_name="editor", max_retries=3)
def editor_extract_job(
    job_id: str,
    input_path: str,
    password: str | None = None,
    source_tracker: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        cleanup_paths(input_path)
        return

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message="Editor extraction started",
    )

    try:
        result = extract_document(input_path, password)

        if isinstance(result, dict):
            result["source_tracker"] = source_tracker or input_path
            result["upright_tracker"] = result.get("upright_tracker") or source_tracker or input_path

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result=result,
            message="Editor extraction completed",
        )
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Editor extraction failed",
        )
        raise
    finally:
        cleanup_paths(input_path)

@dramatiq.actor(queue_name="editor", max_retries=3)
def editor_compile_job(
    job_id: str,
    input_path: str,
    pages_json_path: str,
) -> None:
    job = get_job(job_id)
    if job is None:
        cleanup_paths(input_path, pages_json_path)
        return

    output_pdf_path = temp_file_path(prefix="pdfnest-editor-output-", suffix=".pdf")

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message="Editor compile started",
    )

    try:
        compile_document(input_path, output_pdf_path, pages_json_path)
        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "artifact_path": output_pdf_path,
            },
            message="Editor compile completed",
        )
    except Exception as exc:
        cleanup_paths(output_pdf_path)
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Editor compile failed",
        )
        raise
    finally:
        cleanup_paths(input_path, pages_json_path)
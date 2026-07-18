from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import dramatiq
import json

from app.api.tools.markup.document import process_markup_pdf
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

@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_highlight_job(job_id: str, input_path: str, payload_path: str) -> None:
    _run_markup_job(job_id, input_path, payload_path, action="highlight")


@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_underline_job(job_id: str, input_path: str, payload_path: str) -> None:
    _run_markup_job(job_id, input_path, payload_path, action="underline")


@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_strikeout_job(job_id: str, input_path: str, payload_path: str) -> None:
    _run_markup_job(job_id, input_path, payload_path, action="strikeout")


def _run_markup_job(job_id: str, input_path: str, payload_path: str, action: str) -> None:
    job = get_job(job_id)
    if job is None:
        cleanup_paths(input_path, payload_path)
        return

    output_pdf_path = temp_file_path(prefix=f"pdfnest-{action}-output-", suffix=".pdf")

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message=f"{action.title()} job started",
    )

    try:
        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        boxes = payload.get("boxes", []) or []
        mode = payload.get("mode", "smart")
        file_password = payload.get("file_password")

        def on_progress(done: int, total: int) -> None:
            progress = int((done / max(1, total)) * 100)
            update_job(
                job_id,
                progress=progress,
                message=f"{action.title()} processing {done}/{total}",
            )

        process_markup_pdf(
            input_path=input_path,
            output_path=output_pdf_path,
            boxes=boxes,
            action=action,  # type: ignore[arg-type]
            mode=mode,      # type: ignore[arg-type]
            password=file_password,
            progress_callback=on_progress,
        )

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "artifact_path": output_pdf_path,
                "action": action,
            },
            message=f"{action.title()} job completed",
        )

    except Exception as exc:
        cleanup_paths(output_pdf_path)
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message=f"{action.title()} job failed",
        )
        raise

    finally:
        cleanup_paths(input_path, payload_path)
        cleanup_paths(input_path, payload_path)
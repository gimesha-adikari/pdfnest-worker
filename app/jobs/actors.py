from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dramatiq

from app.api.tools.editor.document import compile_document, extract_document
from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.api.tools.markup.document import process_markup_pdf
from app.core.broker import broker  # noqa: F401
from app.core.storage import build_key, download_to_path, upload_path
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
        source_key: str,
        password: str | None = None,
        source_name: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    input_suffix = Path(source_name or source_key).suffix or ".pdf"
    input_path = temp_file_path(prefix="pdfnest-source-", suffix=input_suffix)

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message="Editor extraction started",
    )

    try:
        download_to_path(source_key, input_path)
        result = extract_document(input_path, password)

        if isinstance(result, dict):
            result["source_tracker"] = source_key
            result["upright_tracker"] = result.get("upright_tracker") or source_key

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
        source_key: str,
        pages_json_key: str,
        source_name: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    input_suffix = Path(source_name or source_key).suffix or ".pdf"
    input_path = temp_file_path(prefix="pdfnest-source-", suffix=input_suffix)
    pages_json_path = temp_file_path(prefix="pdfnest-layout-", suffix=".json")
    output_pdf_path = temp_file_path(prefix="pdfnest-editor-output-", suffix=".pdf")

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message="Editor compile started",
    )

    try:
        download_to_path(source_key, input_path)
        download_to_path(pages_json_key, pages_json_path)

        compile_document(input_path, output_pdf_path, pages_json_path)

        output_key = build_key("jobs/editor/output", suffix=".pdf")
        upload_path(output_pdf_path, output_key, content_type="application/pdf")

        download_name = f"edited_{Path(source_name or 'document.pdf').stem}.pdf"

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "artifact_key": output_key,
                "artifact_name": download_name,
            },
            message="Editor compile completed",
        )
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Editor compile failed",
        )
        raise
    finally:
        cleanup_paths(input_path, pages_json_path, output_pdf_path)


@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_highlight_job(job_id: str, source_key: str, payload_key: str, source_name: str | None = None) -> None:
    _run_markup_job(job_id, source_key, payload_key, source_name, action="highlight")


@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_underline_job(job_id: str, source_key: str, payload_key: str, source_name: str | None = None) -> None:
    _run_markup_job(job_id, source_key, payload_key, source_name, action="underline")


@dramatiq.actor(queue_name="markup", max_retries=3)
def markup_strikeout_job(job_id: str, source_key: str, payload_key: str, source_name: str | None = None) -> None:
    _run_markup_job(job_id, source_key, payload_key, source_name, action="strikeout")


def _run_markup_job(
        job_id: str,
        source_key: str,
        payload_key: str,
        source_name: str | None,
        action: str,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    input_suffix = Path(source_name or source_key).suffix or ".pdf"
    input_path = temp_file_path(prefix="pdfnest-source-", suffix=input_suffix)
    payload_path = temp_file_path(prefix=f"pdfnest-{action}-payload-", suffix=".json")
    output_pdf_path = temp_file_path(prefix=f"pdfnest-{action}-output-", suffix=".pdf")

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=0,
        message=f"{action.title()} job started",
    )

    try:
        download_to_path(source_key, input_path)
        download_to_path(payload_key, payload_path)

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

        output_key = build_key(f"jobs/markup/{action}/output", suffix=".pdf")
        upload_path(output_pdf_path, output_key, content_type="application/pdf")

        download_name = f"{action}_{Path(source_name or 'document.pdf').stem}.pdf"

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "artifact_key": output_key,
                "artifact_name": download_name,
                "action": action,
            },
            message=f"{action.title()} job completed",
        )
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message=f"{action.title()} job failed",
        )
        raise
    finally:
        cleanup_paths(input_path, payload_path, output_pdf_path)
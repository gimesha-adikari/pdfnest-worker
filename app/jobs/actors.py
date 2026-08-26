from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dramatiq
from dramatiq.errors import Retry
import pymupdf as fitz

from app.api.tools.editor.document import compile_document, extract_document
from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.api.tools.markup.document import process_markup_pdf
from app.api.tools.markdown.service import convert_pdf_to_markdown
from app.core.broker import broker  # noqa: F401
from app.core.storage import build_key, download_to_path, upload_path
from app.jobs.cancellation import JobCancelledException, check_cancellation
from app.jobs.limiter import acquire_lease, release_lease
from app.jobs.models import JobState
from app.jobs.store import get_job, update_job

logger = logging.getLogger(__name__)

NON_RETRYABLE_ERRORS = (
    fitz.FileDataError,
    fitz.EmptyFileError,
)


def is_non_retryable_error(exc: Exception) -> bool:
    """Classify only PyMuPDF's verified permanent input errors as non-retryable."""
    return isinstance(exc, NON_RETRYABLE_ERRORS)


@dramatiq.actor(queue_name="default", max_retries=3)
def test_job(job_id: str, payload: dict[str, Any] | None = None) -> None:
    job = get_job(job_id)
    if job is None:
        return

    check_cancellation(job_id)

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


@dramatiq.actor(queue_name="editor", max_retries=3, time_limit=600_000)
def editor_extract_job(
        job_id: str,
        source_key: str,
        password: str | None = None,
        source_name: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    try:
        check_cancellation(job_id)
    except JobCancelledException as exc:
        logger.info("Editor extract job %s cancelled before start: %s", job_id, exc)
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), message="Job cancelled")
        return

    owner_identity = (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    acquired, reason = acquire_lease(job_id, owner_identity)
    if not acquired:
        logger.warning("Execution capacity full (%s) for job %s. Retrying...", reason, job_id)
        raise Retry(message=f"Execution capacity full: {reason}", delay=5000)

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
        check_cancellation(job_id)
        download_to_path(source_key, input_path)
        check_cancellation(job_id)
        result = extract_document(input_path, password, cancellation_check=lambda: check_cancellation(job_id))
        check_cancellation(job_id)

        # The extract layout is public editor data. Storage keys remain inside
        # the worker lifecycle and must not be returned as source trackers.

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result=result,
            message="Editor extraction completed",
        )
    except JobCancelledException as exc:
        logger.info("Editor extract job %s cancelled: %s", job_id, exc)
        update_job(
            job_id,
            status=JobState.cancelled,
            finished_at=datetime.now(timezone.utc),
            message="Editor extraction cancelled",
        )
        return
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Editor extraction failed",
        )
        if is_non_retryable_error(exc):
            logger.warning("Non-retryable PDF error in job %s: %s", job_id, exc)
            return
        raise
    finally:
        release_lease(job_id, owner_identity)
        cleanup_paths(input_path)


@dramatiq.actor(queue_name="editor", max_retries=3, time_limit=600_000)
def editor_compile_job(
        job_id: str,
        source_key: str,
        pages_json_key: str,
        source_name: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    try:
        check_cancellation(job_id)
    except JobCancelledException as exc:
        logger.info("Editor compile job %s cancelled before start: %s", job_id, exc)
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), message="Job cancelled")
        return

    owner_identity = (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    acquired, reason = acquire_lease(job_id, owner_identity)
    if not acquired:
        logger.warning("Execution capacity full (%s) for job %s. Retrying...", reason, job_id)
        raise Retry(message=f"Execution capacity full: {reason}", delay=5000)

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
        check_cancellation(job_id)
        download_to_path(source_key, input_path)
        download_to_path(pages_json_key, pages_json_path)
        check_cancellation(job_id)

        compile_document(input_path, output_pdf_path, pages_json_path, cancellation_check=lambda: check_cancellation(job_id))
        check_cancellation(job_id)

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
    except JobCancelledException as exc:
        logger.info("Editor compile job %s cancelled: %s", job_id, exc)
        update_job(
            job_id,
            status=JobState.cancelled,
            finished_at=datetime.now(timezone.utc),
            message="Editor compile cancelled",
        )
        return
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="Editor compile failed",
        )
        if is_non_retryable_error(exc):
            logger.warning("Non-retryable PDF error in job %s: %s", job_id, exc)
            return
        raise
    finally:
        release_lease(job_id, owner_identity)
        cleanup_paths(input_path, pages_json_path, output_pdf_path)


@dramatiq.actor(queue_name="markup", max_retries=3, time_limit=900_000)
def markup_highlight_job(job_id: str, source_key: str, payload_key: str, source_name: str | None = None) -> None:
    _run_markup_job(job_id, source_key, payload_key, source_name, action="highlight")


@dramatiq.actor(queue_name="markup", max_retries=3, time_limit=900_000)
def markup_underline_job(job_id: str, source_key: str, payload_key: str, source_name: str | None = None) -> None:
    _run_markup_job(job_id, source_key, payload_key, source_name, action="underline")


@dramatiq.actor(queue_name="markup", max_retries=3, time_limit=900_000)
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

    try:
        check_cancellation(job_id)
    except JobCancelledException as exc:
        logger.info("Markup job %s cancelled before start: %s", job_id, exc)
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), message="Job cancelled")
        return

    owner_identity = (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    acquired, reason = acquire_lease(job_id, owner_identity)
    if not acquired:
        logger.warning("Execution capacity full (%s) for job %s. Retrying...", reason, job_id)
        raise Retry(message=f"Execution capacity full: {reason}", delay=5000)

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
        check_cancellation(job_id)
        download_to_path(source_key, input_path)
        download_to_path(payload_key, payload_path)
        check_cancellation(job_id)

        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        boxes = payload.get("boxes", []) or []
        mode = payload.get("mode", "smart")
        file_password = payload.get("file_password")

        def on_progress(done: int, total: int) -> None:
            check_cancellation(job_id)
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
        check_cancellation(job_id)

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
    except JobCancelledException as exc:
        logger.info("Markup job %s cancelled: %s", job_id, exc)
        update_job(
            job_id,
            status=JobState.cancelled,
            finished_at=datetime.now(timezone.utc),
            message=f"{action.title()} job cancelled",
        )
        return
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message=f"{action.title()} job failed",
        )
        if is_non_retryable_error(exc):
            logger.warning("Non-retryable PDF error in markup job %s: %s", job_id, exc)
            return
        raise
    finally:
        release_lease(job_id, owner_identity)
        cleanup_paths(input_path, payload_path, output_pdf_path)


@dramatiq.actor(queue_name="conversion", max_retries=3)
def pdf_to_markdown_job(
    job_id: str,
    source_key: str,
    file_password: str | None = None,
    lang: str = "eng",
    include_annotations: bool = False,
    embed_images: bool = False,
    source_name: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    check_cancellation(job_id)
    owner_identity = getattr(job, "owner_identity", None) or "anonymous"
    acquire_lease(job_id, owner_identity)

    update_job(
        job_id,
        status=JobState.running,
        started_at=datetime.now(timezone.utc),
        progress=5,
        message="Starting PDF to Markdown job",
    )

    input_path = temp_file_path("md-job-input-", ".pdf")
    output_md_path = temp_file_path("md-job-output-", ".md")

    try:
        check_cancellation(job_id)
        download_to_path(source_key, input_path)

        def on_progress(pct: int, msg: str) -> None:
            check_cancellation(job_id)
            update_job(job_id, progress=pct, message=msg)

        markdown_text = convert_pdf_to_markdown(
            input_path=input_path,
            password=file_password,
            lang=lang,
            include_annotations=include_annotations,
            embed_images=embed_images,
            cancellation_check=lambda: check_cancellation(job_id),
            progress_cb=on_progress,
        )

        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        check_cancellation(job_id)

        output_key = build_key("jobs/markdown/output", suffix=".md")
        upload_path(output_md_path, output_key, content_type="text/markdown; charset=utf-8")

        download_name = f"{Path(source_name or 'document.pdf').stem}.md"

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result={
                "artifact_key": output_key,
                "artifact_name": download_name,
            },
            message="PDF to Markdown conversion completed",
        )
    except JobCancelledException as exc:
        logger.info("PDF to Markdown job %s cancelled: %s", job_id, exc)
        update_job(
            job_id,
            status=JobState.cancelled,
            finished_at=datetime.now(timezone.utc),
            message="PDF to Markdown job cancelled",
        )
        return
    except Exception as exc:
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
            message="PDF to Markdown job failed",
        )
        if is_non_retryable_error(exc):
            logger.warning("Non-retryable PDF error in markdown job %s: %s", job_id, exc)
            return
        raise
    finally:
        release_lease(job_id, owner_identity)
        cleanup_paths(input_path, output_md_path)

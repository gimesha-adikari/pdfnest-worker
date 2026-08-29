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
from app.core.config import validate_runtime_config
from app.core.storage import build_key, delete_object, download_to_path, upload_path, upload_text
from app.core.actor_heartbeat import start_actor_heartbeat
from app.jobs.cancellation import JobCancelledException, check_cancellation
from app.jobs.limiter import acquire_lease, release_lease
from app.jobs.models import JobState
from app.jobs.store import claim_job, get_job, update_job

logger = logging.getLogger(__name__)

validate_runtime_config()
_actor_heartbeat_stop = start_actor_heartbeat()

NON_RETRYABLE_ERRORS = (
    fitz.FileDataError,
    fitz.EmptyFileError,
)


def is_non_retryable_error(exc: Exception) -> bool:
    """Classify only PyMuPDF's verified permanent input errors as non-retryable."""
    return isinstance(exc, NON_RETRYABLE_ERRORS)


def _cleanup_input_objects(keys: list[str]) -> None:
    for key in keys:
        if not key:
            continue
        try:
            delete_object(key)
        except Exception:
            logger.warning("OCR V2 input cleanup failed for key %s", key, exc_info=True)


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


@dramatiq.actor(queue_name="ocr", max_retries=0, time_limit=3_600_000)
def ocr_v2_job(
        job_id: str,
        source_key: str,
        source_name: str,
        language: str,
        routing_policy: str,
        profile: str = "OCR_TEXT_V2",
        source_files: list[dict[str, str]] | None = None,
) -> None:
    """Run one durable OCR V2 job through the existing Dramatiq worker."""
    if profile == "SEARCHABLE_PDF_V2":
        _run_searchable_pdf_job(job_id, language, source_files or [], source_name)
        return
    job = get_job(job_id)
    if job is None:
        _cleanup_input_objects([source_key])
        return
    if job.status == JobState.running:
        return
    if job.status in {JobState.succeeded, JobState.failed, JobState.cancelled, JobState.cancel_requested}:
        _cleanup_input_objects([source_key])
        return
    if not source_key or source_key.startswith("/") or "\\" in source_key or ".." in source_key.split("/"):
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="OCR V2 input reference is invalid.", error_code="INVALID_INPUT", message="OCR V2 job input validation failed")
        return

    owner_identity = job.owner_identity or (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    input_path = temp_file_path(prefix="pdfnest-ocr-v2-input-", suffix=Path(source_name or source_key).suffix or ".pdf")
    result_key = f"jobs/ocr_v2/results/{job_id}.json"
    warnings: list[str] = []
    failed_pages: list[int] = []
    page_statuses: dict[str, str] = {}
    acquired = False
    claimed = False

    try:
        check_cancellation(job_id)
        if claim_job(job_id) is None:
            return
        claimed = True
        check_cancellation(job_id)
        acquired, reason = acquire_lease(job_id, owner_identity)
        if not acquired:
            update_job(
                job_id,
                status=JobState.failed,
                finished_at=datetime.now(timezone.utc),
                error="OCR V2 execution capacity is unavailable.",
                error_code="ENGINE_UNAVAILABLE" if reason != "REDIS_ERROR" else "TASK_STORAGE_UNAVAILABLE",
                message="OCR V2 job could not acquire execution capacity",
            )
            return

        update_job(job_id, progress=0, current_page=None, message="OCR V2 job started")
        check_cancellation(job_id)
        download_to_path(source_key, input_path)
        check_cancellation(job_id)

        # Importing the API projection here keeps the actor dependent on the
        # same product-safe response mapper without making the job module part
        # of the FastAPI startup import cycle.
        from app.api.ocr_v2.router import _response, _route_policy
        from app.api.ocr_v2.schemas import OCRV2WorkerRequest
        from app.core.ocr_v2 import OCRV2Worker
        from app.core.ocr_v2.adapters import PPOCRv6MediumAdapter
        from app.core.ocr_v2.validation import OCRProfile

        contract = OCRV2WorkerRequest(
            request_id=job_id,
            profile="OCR_TEXT_V2",
            language=language,
            routing_policy=routing_policy,
        )
        if contract.routing_policy in {"AUTO", "QUALITY", "GEOMETRY"} and not PPOCRv6MediumAdapter().availability().available:
            warnings.append("ENGINE_FALLBACK:PP_OCR_UNAVAILABLE_TO_TESSERACT")
        worker = OCRV2Worker(route_policy=_route_policy(contract.routing_policy))

        def on_page(done: int, total: int, page: object) -> None:
            check_cancellation(job_id)
            if getattr(getattr(page, "status", None), "value", "") == "FAILED":
                failed_pages.append(int(page.page_index))
            page_statuses[str(page.page_index)] = getattr(getattr(page, "status", None), "value", "FAILED")
            # The orchestration callback is intentionally small and durable:
            # status polling receives counts and the current page, while the
            # final product result remains in object storage.
            update_job(
                job_id,
                progress=int((done / max(1, total)) * 100),
                total_pages=total,
                completed_pages=done - len(failed_pages),
                failed_pages=failed_pages,
                current_page=page.page_index,
                page_statuses=page_statuses,
                warnings=warnings,
                message=f"OCR V2 processing page {done}/{total}",
            )

        result = worker.process_document(
            input_path,
            language=contract.language,
            profile=OCRProfile.OCR_TEXT_V2,
            cancellation_check=lambda: check_cancellation(job_id),
            page_progress_callback=on_page,
        )
        response = _response(result, contract, warnings)
        result_payload = response.model_dump()
        upload_text(json.dumps(result_payload, ensure_ascii=False), result_key, content_type="application/json")
        failed_pages = [page.page_index for page in result.pages if page.status.value == "FAILED"]
        completed_pages = len(result.pages) - len(failed_pages)
        if response.status == "SUCCEEDED":
            update_job(
                job_id,
                status=JobState.succeeded,
                finished_at=datetime.now(timezone.utc),
                progress=100,
                total_pages=result.source.page_count,
                completed_pages=completed_pages,
                failed_pages=failed_pages,
                current_page=None,
                page_statuses={str(page.page_index): page.status.value for page in result.pages},
                warnings=response.warnings,
                result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}.json"},
                message="OCR V2 job completed",
            )
        else:
            update_job(
                job_id,
                status=JobState.failed,
                finished_at=datetime.now(timezone.utc),
                progress=int((len(result.pages) / max(1, result.source.page_count)) * 100),
                total_pages=result.source.page_count,
                completed_pages=completed_pages,
                failed_pages=failed_pages,
                current_page=None,
                page_statuses={str(page.page_index): page.status.value for page in result.pages},
                warnings=response.warnings,
                error=response.error.message if response.error else "OCR V2 job failed",
                error_code=response.error.code if response.error else "ENGINE_FAILURE",
                result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}.json"},
                message="OCR V2 job completed with page failures",
            )
    except JobCancelledException:
        update_job(
            job_id,
            status=JobState.cancelled,
            finished_at=datetime.now(timezone.utc),
            current_page=None,
            error_code="CANCELLED",
            message="OCR V2 job cancelled",
        )
    except Exception as exc:
        logger.exception("OCR V2 job %s failed", job_id)
        update_job(
            job_id,
            status=JobState.failed,
            finished_at=datetime.now(timezone.utc),
            error="OCR V2 job failed.",
            error_code="ENGINE_FAILURE",
            message="OCR V2 job failed",
        )
    finally:
        if acquired:
            release_lease(job_id, owner_identity)
        if claimed:
            _cleanup_input_objects([source_key])
        cleanup_paths(input_path)


def _run_searchable_pdf_job(
    job_id: str,
    language: str,
    source_files: list[dict[str, str]],
    source_name: str,
) -> None:
    """Run ordered images through real word-level OCR and PDF rendering."""
    job = get_job(job_id)
    if job is None:
        _cleanup_input_objects([str(item.get("source_key", "")).strip() for item in source_files if isinstance(item, dict)])
        return
    if not source_files or any(not str(item.get("source_key", "")).strip() for item in source_files):
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="Ordered image input references are invalid.", error_code="INVALID_INPUT", message="Searchable PDF input validation failed")
        return

    owner_identity = job.owner_identity or (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    source_keys = [str(item["source_key"]) for item in source_files]
    if job.status == JobState.running:
        return
    if job.status in {JobState.succeeded, JobState.failed, JobState.cancelled, JobState.cancel_requested}:
        _cleanup_input_objects(source_keys)
        return
    local_inputs: list[str] = []
    source_pdf = temp_file_path(prefix="pdfnest-searchable-source-", suffix=".pdf")
    output_pdf = temp_file_path(prefix="pdfnest-searchable-output-", suffix=".pdf")
    result_key = f"jobs/ocr_v2/searchable_pdf/{job_id}.pdf"
    failed_pages: list[int] = []
    page_statuses: dict[str, str] = {}
    acquired = False
    claimed = False

    try:
        check_cancellation(job_id)
        if claim_job(job_id) is None:
            return
        claimed = True
        check_cancellation(job_id)
        acquired, reason = acquire_lease(job_id, owner_identity)
        if not acquired:
            update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="OCR V2 execution capacity is unavailable.", error_code="ENGINE_UNAVAILABLE" if reason != "REDIS_ERROR" else "TASK_STORAGE_UNAVAILABLE", message="Searchable PDF job could not acquire execution capacity")
            return
        update_job(job_id, status=JobState.running, started_at=datetime.now(timezone.utc), progress=0, total_pages=len(source_files), message="Searchable PDF V2 job started")

        for index, item in enumerate(source_files):
            check_cancellation(job_id)
            suffix = Path(item.get("source_name", "image.png")).suffix or ".img"
            local_path = temp_file_path(prefix=f"pdfnest-searchable-input-{index}-", suffix=suffix)
            download_to_path(str(item["source_key"]), local_path)
            local_inputs.append(local_path)

        from app.core.ocr_v2 import OCRV2Worker
        from app.core.ocr_v2.image_pages import build_image_source_pdf
        from app.core.ocr_v2.renderers.searchable_pdf import SearchablePdfRenderer
        from app.core.ocr_v2.routing import RoutePolicy
        from app.core.ocr_v2.validation import OCRProfile

        build_image_source_pdf(local_inputs, source_pdf)
        # Searchable PDF requires genuine word geometry; PP-OCR's current
        # line-level contract is intentionally not eligible for this profile.
        worker = OCRV2Worker(route_policy=RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2"))

        def on_page(done: int, total: int, page: object) -> None:
            check_cancellation(job_id)
            status = getattr(getattr(page, "status", None), "value", "FAILED")
            page_statuses[str(page.page_index)] = status
            if status == "FAILED":
                failed_pages.append(int(page.page_index))
            update_job(job_id, progress=int((done / max(1, total)) * 90), total_pages=total, completed_pages=done - len(failed_pages), failed_pages=failed_pages, current_page=page.page_index, page_statuses=page_statuses, message=f"Searchable PDF V2 processing page {done}/{total}")

        result = worker.process_document(source_pdf, language=language, profile=OCRProfile.SEARCHABLE_PDF_V2, cancellation_check=lambda: check_cancellation(job_id), page_progress_callback=on_page)
        check_cancellation(job_id)
        if not result.validation.valid:
            issue_codes = ";".join(issue.code for issue in result.validation.issues)
            raise ValueError(f"PROFILE_NOT_ELIGIBLE:{issue_codes}")
        SearchablePdfRenderer().render(source_pdf, result, output_pdf)
        check_cancellation(job_id)
        upload_path(output_pdf, result_key, content_type="application/pdf")
        size = Path(output_pdf).stat().st_size
        update_job(job_id, status=JobState.succeeded, finished_at=datetime.now(timezone.utc), progress=100, total_pages=len(result.pages), completed_pages=len(result.pages), failed_pages=[], current_page=None, page_statuses={str(page.page_index): page.status.value for page in result.pages}, result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}-searchable.pdf", "artifact_content_type": "application/pdf", "artifact_size": size}, message="Searchable PDF V2 job completed")
    except JobCancelledException:
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), current_page=None, error_code="CANCELLED", message="Searchable PDF V2 job cancelled")
    except Exception as exc:
        logger.exception("Searchable PDF V2 job %s failed", job_id)
        error_code = "PROFILE_NOT_ELIGIBLE" if str(exc).startswith("PROFILE_NOT_ELIGIBLE:") else "ENGINE_FAILURE"
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), current_page=None, error="Searchable PDF V2 job failed.", error_code=error_code, message="Searchable PDF V2 job failed")
    finally:
        if acquired:
            release_lease(job_id, owner_identity)
        if claimed:
            _cleanup_input_objects(source_keys)
        cleanup_paths(*local_inputs, source_pdf, output_pdf)
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

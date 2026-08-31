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

from app.api.tools.editor.document import compile_document, extract_document, extract_document_v2
from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.api.tools.markup.document import process_markup_pdf
from app.api.tools.markdown.service import convert_pdf_to_markdown
from app.core.broker import broker  # noqa: F401
from app.core.config import validate_runtime_config
from app.core.ocr_v2.diagnostics import emit_searchable_diagnostic, retain_failed_render_artifacts, safe_exception_message
from app.core.ocr_v2.errors import EngineUnavailableError, OCRTimeoutError, RenderingNotEligibleError
from app.core.ocr_v2.errors import MarkupError, TextNotFoundError, WordGeometryUnavailableError, AnnotationWriteError
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


def _searchable_failure_code(exc: Exception, stage: str) -> str:
    """Map a searchable-PDF failure to a safe public contract code.

    The exception itself stays in the actor log for diagnosis, while the job
    record receives only a stable classification that the backend can project
    safely to the product UI.
    """
    if isinstance(exc, RenderingNotEligibleError):
        return "PDF_RENDER_FAILURE"
    if isinstance(exc, EngineUnavailableError):
        return "ENGINE_UNAVAILABLE"
    if isinstance(exc, OCRTimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ValueError) and str(exc).startswith("PROFILE_NOT_ELIGIBLE:"):
        return "PROFILE_NOT_ELIGIBLE"
    if stage == "INPUT_DOWNLOAD":
        return "INPUT_DOWNLOAD"
    if stage == "IMAGE_NORMALIZATION":
        return "INVALID_INPUT"
    if stage == "ARTIFACT_PERSISTENCE":
        return "TASK_STORAGE_UNAVAILABLE"
    return "ENGINE_FAILURE"


def _searchable_failure_message(code: str, stage: str) -> str:
    """Return safe durable diagnostics without storing exception text."""
    if code == "INPUT_DOWNLOAD":
        return "We couldn't access one of your uploaded images. Upload the images again to start over."
    return f"Searchable PDF V2 job failed during {stage} ({code})."


def _raise_primary_page_failure(result: object) -> None:
    """Preserve a primary engine failure before profile validation runs.

    A failed OCR page necessarily leaves the requested searchable-PDF
    capabilities absent.  That downstream profile consequence must not mask
    the engine/runtime failure that caused it.
    """
    for page in getattr(result, "pages", ()):
        if getattr(getattr(page, "status", None), "value", getattr(page, "status", None)) != "FAILED":
            continue
        failure_code = getattr(page, "failure_code", None)
        if failure_code == "EngineUnavailableError":
            raise EngineUnavailableError("OCR engine was unavailable while processing a searchable-PDF page")


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
        markup_action: str | None = None,
        markup_mode: str = "smart",
        markup_query: str | None = None,
        markup_color: str = "#FFFF00",
        language_mode: str = "EXPLICIT",
        languages: list[str] | None = None,
        language_usage: dict[str, float] | None = None,
) -> None:
    """Run one durable OCR V2 job through the existing Dramatiq worker."""
    if profile == "SEARCHABLE_PDF_V2":
        _run_searchable_pdf_job(job_id, language, source_files or [], source_name, language_mode, languages or [], language_usage or {})
        return
    if profile in {"DOCUMENT_EXTRACTION_V2", "PDF_MARKDOWN_V2"}:
        _run_structured_document_job(job_id, source_key, source_name, language, routing_policy, profile, language_mode, languages or [], language_usage or {})
        return
    if profile == "MARKUP_V2":
        _run_markup_v2_job(job_id, source_key, source_name, language, markup_action or "", markup_mode, markup_query or "", markup_color, language_mode, languages or [], language_usage or {})
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
    stage = "JOB_START"

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
    language_mode: str = "EXPLICIT",
    languages: list[str] | None = None,
    language_usage: dict[str, float] | None = None,
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
    normalized_images: tuple[object, ...] = ()
    forensic_metadata: list[dict[str, Any]] = []
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
            stage = "INPUT_DOWNLOAD"
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

        stage = "IMAGE_NORMALIZATION"
        normalized_images = build_image_source_pdf(local_inputs, source_pdf)
        for index, normalized in enumerate(normalized_images):
            metadata = {
                "page_index": index,
                "input_format": getattr(normalized, "format", ""),
                "normalized_width": getattr(normalized, "width", None),
                "normalized_height": getattr(normalized, "height", None),
                "input_mode": getattr(normalized, "input_mode", ""),
                "normalized_mode": getattr(normalized, "normalized_mode", ""),
                "exif_present": getattr(normalized, "exif_present", False),
                "exif_orientation": getattr(normalized, "exif_orientation", None),
                "alpha_present": getattr(normalized, "alpha_present", False),
                "canonical_pdf_width": getattr(normalized, "page_width", None),
                "canonical_pdf_height": getattr(normalized, "page_height", None),
            }
            forensic_metadata.append(metadata)
            emit_searchable_diagnostic(event="SOURCE_PAGE_METADATA", job_id=job_id, substage="PDF_RENDER_SOURCE_SETUP", fields=metadata)
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

        stage = "OCR"
        result = worker.process_document(source_pdf, language=language, language_mode=language_mode, languages=languages or [], language_usage=language_usage or {}, profile=OCRProfile.SEARCHABLE_PDF_V2, cancellation_check=lambda: check_cancellation(job_id), page_progress_callback=on_page)
        check_cancellation(job_id)
        for page in result.pages:
            valid_word_count = sum(
                1
                for token in page.tokens
                if token.text.strip()
                and token.bbox.width > 0
                and token.bbox.height > 0
                and token.bbox.x >= 0
                and token.bbox.y >= 0
                and token.bbox.x1 <= page.geometry.width + 1e-6
                and token.bbox.y1 <= page.geometry.height + 1e-6
            )
            page_metadata = {
                "page_index": page.page_index,
                "ocr_token_count": len(page.tokens),
                "valid_word_token_count": valid_word_count,
                "reading_order_token_count": len(page.reading_order),
            }
            for source_metadata in forensic_metadata:
                if source_metadata["page_index"] == page.page_index:
                    source_metadata.update(page_metadata)
                    break
            emit_searchable_diagnostic(event="OCR_PAGE_METADATA", job_id=job_id, substage="PDF_RENDER_PROFILE_CHECK", fields=page_metadata)
        _raise_primary_page_failure(result)
        stage = "PROFILE_VALIDATION"
        if not result.validation.valid:
            issue_codes = ";".join(issue.code for issue in result.validation.issues)
            raise ValueError(f"PROFILE_NOT_ELIGIBLE:{issue_codes}")
        stage = "PDF_RENDER"
        SearchablePdfRenderer().render(source_pdf, result, output_pdf, job_id=job_id)
        check_cancellation(job_id)
        stage = "ARTIFACT_PERSISTENCE"
        upload_path(output_pdf, result_key, content_type="application/pdf")
        size = Path(output_pdf).stat().st_size
        update_job(job_id, status=JobState.succeeded, finished_at=datetime.now(timezone.utc), progress=100, total_pages=len(result.pages), completed_pages=len(result.pages), failed_pages=[], current_page=None, page_statuses={str(page.page_index): page.status.value for page in result.pages}, result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}-searchable.pdf", "artifact_content_type": "application/pdf", "artifact_size": size}, message="Searchable PDF V2 job completed")
    except JobCancelledException:
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), current_page=None, error_code="CANCELLED", message="Searchable PDF V2 job cancelled")
    except Exception as exc:
        error_code = _searchable_failure_code(exc, stage)
        safe_message = _searchable_failure_message(error_code, stage)
        failure_fields = {
            "event": "SEARCHABLE_FAILURE",
            "job_id": job_id,
            "profile": "SEARCHABLE_PDF_V2",
            "page_count": len(source_files),
            "render_substage": getattr(exc, "substage", None) or stage,
            "exception_class": type(exc).__name__,
            "safe_exception_message": safe_exception_message(exc),
        }
        if error_code == "PDF_RENDER_FAILURE":
            substage = getattr(exc, "substage", None) or stage
            reason_code = getattr(exc, "reason_code", None) or "RENDER_FAILURE"
            failure_fields["render_substage"] = substage
            failure_fields["reason_code"] = reason_code
            emit_searchable_diagnostic(
                event="SEARCHABLE_FAILURE",
                job_id=job_id,
                substage=substage,
                fields={
                    "exception_class": type(exc).__name__,
                    "reason_code": reason_code,
                    "exception_message": safe_exception_message(exc),
                    "page_count": len(source_files),
                },
            )
            retain_failed_render_artifacts(
                job_id=job_id,
                source_pdf=source_pdf,
                output_pdf=output_pdf,
                metadata=forensic_metadata,
            )
        logger.exception("OCR_V2_SEARCHABLE_FAILURE %s", json.dumps(failure_fields, sort_keys=True))
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), current_page=None, error=safe_message, error_code=error_code, message=safe_message)
    finally:
        if acquired:
            release_lease(job_id, owner_identity)
        if claimed:
            _cleanup_input_objects(source_keys)
        cleanup_paths(*local_inputs, source_pdf, output_pdf)


def _run_structured_document_job(
    job_id: str,
    source_key: str,
    source_name: str,
    language: str,
    routing_policy: str,
    profile: str,
    language_mode: str = "EXPLICIT",
    languages: list[str] | None = None,
    language_usage: dict[str, float] | None = None,
) -> None:
    """Process a PDF into the canonical structured result through the OCR queue."""
    job = get_job(job_id)
    if job is None:
        _cleanup_input_objects([source_key])
        return
    if not source_key or source_key.startswith("/") or "\\" in source_key or ".." in source_key.split("/"):
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="Structured input reference is invalid.", error_code="INVALID_INPUT", message="Structured document input validation failed")
        return
    if job.status == JobState.running:
        return
    if job.status in {JobState.succeeded, JobState.failed, JobState.cancelled, JobState.cancel_requested}:
        _cleanup_input_objects([source_key])
        return

    owner_identity = job.owner_identity or (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    input_path = temp_file_path(prefix="pdfnest-structured-input-", suffix=Path(source_name or source_key).suffix or ".pdf")
    result_key = f"jobs/ocr_v2/structured/{profile.lower()}/{job_id}.json"
    acquired = False
    claimed = False
    stage = "JOB_START"
    try:
        check_cancellation(job_id)
        if claim_job(job_id) is None:
            return
        claimed = True
        acquired, reason = acquire_lease(job_id, owner_identity)
        if not acquired:
            update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="Structured execution capacity is unavailable.", error_code="ENGINE_UNAVAILABLE" if reason != "REDIS_ERROR" else "TASK_STORAGE_UNAVAILABLE", message="Structured job could not acquire execution capacity")
            return
        update_job(job_id, progress=0, current_page=None, message=f"{profile} job started")
        download_to_path(source_key, input_path)
        check_cancellation(job_id)
        from app.core.ocr_v2.structured import StructuredDocumentProcessor, render_structured_markdown

        stage = "STRUCTURED_PROCESSING"

        def on_page(done: int, total: int, page: object) -> None:
            check_cancellation(job_id)
            update_job(
                job_id,
                progress=int((done / max(1, total)) * 90),
                total_pages=total,
                completed_pages=done,
                failed_pages=[],
                current_page=getattr(page, "page_index", None),
                page_statuses={str(index): "SUCCESS" for index in range(done)},
                warnings=list(getattr(page, "warnings", ())),
                message=f"{profile} processing page {done}/{total}",
            )

        result = StructuredDocumentProcessor().process_document(
            input_path,
            language=language,
            language_mode=language_mode,
            languages=languages or [],
            language_usage=language_usage or {},
            routing_policy=routing_policy,
            cancellation_check=lambda: check_cancellation(job_id),
            page_progress_callback=on_page,
        )
        result_payload = result.to_dict()
        if profile == "PDF_MARKDOWN_V2":
            result_payload["markdown"] = render_structured_markdown(result)
        serialized_result = json.dumps(result_payload, ensure_ascii=False)
        from app.core.ocr_v2.structured import structured_max_output_bytes
        serialized_size = len(serialized_result.encode("utf-8"))
        if serialized_size > structured_max_output_bytes():
            raise ValueError("structured OCR result exceeds the configured output limit")
        upload_text(serialized_result, result_key, content_type="application/json")
        page_statuses = {str(page.page_index): page.status for page in result.pages}
        if result.validation.get("valid") and all(page.status in {"SUCCESS", "BLANK"} for page in result.pages):
            update_job(
                job_id,
                status=JobState.succeeded,
                finished_at=datetime.now(timezone.utc),
                progress=100,
                total_pages=len(result.pages),
                completed_pages=sum(page.status in {"SUCCESS", "BLANK"} for page in result.pages),
                failed_pages=[],
                current_page=None,
                page_statuses=page_statuses,
                warnings=list(result.warnings),
                result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}.json", "artifact_content_type": "application/json", "artifact_size": serialized_size},
                message=f"{profile} job completed",
            )
        else:
            update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), progress=100, total_pages=len(result.pages), completed_pages=sum(page.status in {"SUCCESS", "BLANK"} for page in result.pages), failed_pages=[page.page_index for page in result.pages if page.status not in {"SUCCESS", "BLANK"}], current_page=None, page_statuses=page_statuses, warnings=list(result.warnings), error="Structured document result did not pass validation.", error_code="STRUCTURED_OUTPUT_INVALID", result={"artifact_key": result_key, "artifact_name": f"{Path(source_name or 'document').stem}.json", "artifact_content_type": "application/json"}, message=f"{profile} validation failed")
    except JobCancelledException:
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), current_page=None, error_code="CANCELLED", message=f"{profile} job cancelled")
    except Exception:
        logger.exception("OCR_V2_STRUCTURED_FAILURE job_id=%s profile=%s stage=%s", job_id, profile, stage)
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), current_page=None, error="Structured document processing failed.", error_code="STRUCTURED_ENGINE_UNAVAILABLE" if stage == "STRUCTURED_PROCESSING" else "ENGINE_FAILURE", message=f"{profile} job failed")
    finally:
        if acquired:
            release_lease(job_id, owner_identity)
        if claimed:
            _cleanup_input_objects([source_key])
        cleanup_paths(input_path)
@dramatiq.actor(queue_name="editor", max_retries=3, time_limit=600_000)
def editor_extract_job(
        job_id: str,
        source_key: str,
        password: str | None = None,
        source_name: str | None = None,
        ocr_v2: bool = False,
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
        def report_editor_page(done: int, total: int, _page: object) -> None:
            if not ocr_v2:
                return
            progress = min(90, 20 + int((done / max(1, total)) * 70))
            update_job(
                job_id,
                progress=progress,
                current_page=done,
                total_pages=total,
                message=f"OCR V2 editor page {done} of {total} analyzed",
            )

        result = (extract_document_v2 if ocr_v2 else extract_document)(
            input_path,
            password,
            cancellation_check=lambda: check_cancellation(job_id),
            page_progress_callback=report_editor_page if ocr_v2 else None,
        )
        check_cancellation(job_id)

        # The extract layout is public editor data. Storage keys remain inside
        # the worker lifecycle and must not be returned as source trackers.

        update_job(
            job_id,
            status=JobState.succeeded,
            finished_at=datetime.now(timezone.utc),
            progress=100,
            result=result,
            message="OCR V2 editor extraction completed" if ocr_v2 else "Editor extraction completed",
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
            error="OCR V2 editor extraction failed." if ocr_v2 else str(exc),
            error_code="ENGINE_UNAVAILABLE" if isinstance(exc, EngineUnavailableError) else ("EDITOR_EXTRACTION_INVALID" if ocr_v2 else None),
            message="OCR V2 editor extraction failed" if ocr_v2 else "Editor extraction failed",
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


def _markup_public_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, EngineUnavailableError):
        return "ENGINE_UNAVAILABLE", "The OCR engine is currently unavailable."
    if isinstance(exc, WordGeometryUnavailableError):
        return "WORD_GEOMETRY_NOT_AVAILABLE", "Text selection is unavailable because genuine word geometry was not produced."
    if isinstance(exc, TextNotFoundError):
        return "TEXT_NOT_FOUND", "The requested text was not found."
    if isinstance(exc, AnnotationWriteError):
        return "ANNOTATION_WRITE_FAILURE", "The requested PDF annotation could not be written."
    if isinstance(exc, MarkupError):
        return "PROFILE_NOT_ELIGIBLE", "The document is not eligible for automatic OCR-aware markup."
    if isinstance(exc, OCRTimeoutError):
        return "TIMEOUT", "Markup processing timed out."
    return "ENGINE_FAILURE", "OCR-aware markup could not be completed."


@dramatiq.actor(queue_name="ocr", max_retries=0, time_limit=900_000)
def _run_markup_v2_job(
        job_id: str,
        source_key: str,
        source_name: str,
        language: str,
        action: str,
        mode: str,
        query: str,
        color: str,
        language_mode: str = "EXPLICIT",
        languages: list[str] | None = None,
        language_usage: dict[str, float] | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        _cleanup_input_objects([source_key])
        return
    owner_identity = job.owner_identity or (job.payload or {}).get("ownerIdentity") or "guest:anonymous"
    input_path = temp_file_path(prefix="pdfnest-ocr-v2-markup-input-", suffix=Path(source_name or source_key).suffix or ".pdf")
    output_path = temp_file_path(prefix="pdfnest-ocr-v2-markup-output-", suffix=".pdf")
    acquired = False
    claimed = False
    try:
        check_cancellation(job_id)
        if claim_job(job_id) is None:
            return
        claimed = True
        acquired, reason = acquire_lease(job_id, owner_identity)
        if not acquired:
            update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error="OCR-aware markup execution capacity is unavailable.", error_code="ENGINE_UNAVAILABLE" if reason != "REDIS_ERROR" else "TASK_STORAGE_UNAVAILABLE", message="Markup V2 could not acquire execution capacity")
            return
        update_job(job_id, status=JobState.running, started_at=datetime.now(timezone.utc), progress=0, total_pages=job.total_pages, message="OCR-aware markup started")
        download_to_path(source_key, input_path)
        from app.core.ocr_v2.markup import MarkupAction, MarkupMode, apply_ocr_markup

        try:
            selected_action = MarkupAction(action)
            selected_mode = MarkupMode(mode)
        except ValueError as exc:
            raise ValueError("invalid markup action or mode") from exc

        cleaned_color = color.strip().lstrip("#")
        if len(cleaned_color) != 6:
            raise ValueError("invalid markup color")
        rgb = tuple(int(cleaned_color[index:index + 2], 16) / 255 for index in (0, 2, 4))

        def on_progress(done: int, total: int) -> None:
            check_cancellation(job_id)
            update_job(job_id, progress=int(done / max(1, total) * 90), total_pages=total, completed_pages=done, current_page=max(0, done - 1), message=f"OCR-aware markup analyzing page {done}/{total}")

        execution = apply_ocr_markup(input_path, output_path, action=selected_action, query=query, language=language, language_mode=language_mode, languages=languages or [], language_usage=language_usage or {}, mode=selected_mode, color=rgb, cancellation_check=lambda: check_cancellation(job_id), progress_callback=on_progress)
        check_cancellation(job_id)
        output_key = f"jobs/ocr_v2/markup/{action}/{job_id}.pdf"
        upload_path(output_path, output_key, content_type="application/pdf")
        artifact_name = f"{action}_{Path(source_name or 'document').stem}.pdf"
        metadata = execution.to_dict()
        result_key = f"jobs/ocr_v2/markup/{action}/{job_id}.json"
        upload_text(json.dumps(metadata, ensure_ascii=False), result_key)
        update_job(job_id, status=JobState.succeeded, finished_at=datetime.now(timezone.utc), progress=100, total_pages=execution.page_count, completed_pages=execution.page_count, current_page=None, page_statuses={str(index): "SUCCESS" for index in range(execution.page_count)}, result={"artifact_key": output_key, "artifact_name": artifact_name, "artifact_content_type": "application/pdf", "metadata_key": result_key, "action": action}, message="OCR-aware markup completed")
    except JobCancelledException:
        update_job(job_id, status=JobState.cancelled, finished_at=datetime.now(timezone.utc), error_code="CANCELLED", message="OCR-aware markup cancelled")
    except Exception as exc:
        code, message = _markup_public_error(exc)
        logger.exception("OCR V2 markup job %s failed", job_id)
        update_job(job_id, status=JobState.failed, finished_at=datetime.now(timezone.utc), error=message, error_code=code, message="OCR-aware markup failed")
    finally:
        if acquired:
            release_lease(job_id, owner_identity)
        if claimed:
            _cleanup_input_objects([source_key])
        cleanup_paths(input_path, output_path)


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

        if payload.get("ocr_v2"):
            from app.api.tools.markup.document import process_markup_pdf_v2_regions
            process_markup_pdf_v2_regions(
                input_path=input_path,
                output_path=output_pdf_path,
                boxes=boxes,
                action=action,  # type: ignore[arg-type]
                mode=mode,      # type: ignore[arg-type]
                password=file_password,
                progress_callback=on_progress,
            )
        else:
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

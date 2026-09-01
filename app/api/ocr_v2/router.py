"""Authenticated private OCR V2 worker API.

The global WorkerAuthMiddleware protects this route. The route accepts a
multipart upload rather than a caller-controlled filesystem path, stages it in
a temporary file, and returns only the backend-safe OCR Text V2 projection.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pymupdf as fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.tools.ocr.languages import get_installed_tesseract_languages, language_name
from app.api.tools.ocr.router import create_route_cancellation_checker
from app.core.ocr_v2.adapters import PPOCRv6MediumAdapter, TesseractAdapter
from app.core.ocr_v2.errors import (
    EngineUnavailableError,
    LanguageDetectionUncertainError,
    OCRTimeoutError,
    WordGeometryUnavailableError,
)
from app.core.ocr_v2.language_policy import OCRLanguageMode, OCRLanguagePolicy
from app.core.ocr_markup_engine import (
    OcrMarkupEngineConfigurationError,
    OcrMarkupEngineUnavailableError,
    execute_ocr_markup_preview,
)
from app.core.ocr_text_engine import (
    OCRTextEngineConfigurationError,
    OCRTextEngineUnavailableError,
    execute_ocr_text,
)
from app.jobs.cancellation import JobCancelledException

from .schemas import (
    OCRV2ErrorResponse,
    OCRV2PageResponse,
    OCRV2Profile,
    OCRV2RoutingPolicy,
    OCRV2MarkupPreviewRequest,
    OCRV2MarkupPreviewResponse,
    OCRV2WorkerRequest,
    OCRV2WorkerResponse,
)


router = APIRouter(prefix="/internal/ocr/v2", tags=["ocr-v2-internal"])
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _request_model(request_id: str, profile: str, language: str | None, routing_policy: str, language_mode: str = "EXPLICIT", languages: list[str] | None = None) -> OCRV2WorkerRequest:
    if profile != OCRV2Profile.OCR_TEXT_V2.value:
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "This endpoint only accepts OCR_TEXT_V2"})
    if not language or not language.strip():
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "An OCR language is required"})
    try:
        return OCRV2WorkerRequest(request_id=request_id, profile=profile, language=language or "eng", language_mode=language_mode, languages=languages or [], routing_policy=routing_policy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_LANGUAGE", "message": "Choose one or more installed OCR languages."}) from exc


def _markup_preview_request_model(request_id: str, profile: str, language: str | None, routing_policy: str, language_mode: str = "EXPLICIT", languages: list[str] | None = None) -> OCRV2MarkupPreviewRequest:
    if profile != OCRV2Profile.MARKUP_V2.value:
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "This endpoint only accepts MARKUP_V2"})
    if not language or not language.strip():
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "An OCR language is required"})
    try:
        return OCRV2MarkupPreviewRequest(
            request_id=request_id,
            profile=profile,
            language=language,
            language_mode=language_mode,
            languages=languages or [],
            routing_policy=routing_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_LANGUAGE", "message": "Choose one or more installed OCR languages."}) from exc


def _max_bytes() -> int:
    value = int(os.getenv("OCR_V2_MAX_BYTES", os.getenv("MAX_REQUEST_BODY_SIZE", str(100 * 1024 * 1024))))
    return max(1, value)


def _max_pages() -> int:
    value = int(os.getenv("OCR_V2_MAX_PAGES", os.getenv("MAX_PAGES_OCR", "150")))
    return max(1, value)


async def _stage_pdf(upload: UploadFile) -> str:
    if upload.content_type not in (None, "", "application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=415, detail={"code": "INVALID_INPUT", "message": "OCR V2 requires a PDF upload"})
    fd, path = tempfile.mkstemp(prefix="pdfnest-ocr-v2-", suffix=".pdf")
    os.close(fd)
    total = 0
    try:
        with open(path, "wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _max_bytes():
                    raise HTTPException(status_code=413, detail={"code": "INVALID_INPUT", "message": "OCR V2 document exceeds the configured size limit"})
                output.write(chunk)
        with open(path, "rb") as source:
            if source.read(5) != b"%PDF-":
                raise HTTPException(status_code=415, detail={"code": "INVALID_INPUT", "message": "Uploaded content is not a PDF"})
        try:
            with fitz.open(path) as document:
                if len(document) == 0 or len(document) > _max_pages():
                    raise HTTPException(status_code=413, detail={"code": "INVALID_INPUT", "message": "OCR V2 document exceeds the configured page limit"})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=415, detail={"code": "INVALID_INPUT", "message": "Uploaded PDF could not be read"}) from exc
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _availability(language: str) -> list[dict[str, object]]:
    requested = OCRLanguagePolicy.from_request(language)
    tesseract = TesseractAdapter("eng" if requested.mode is OCRLanguageMode.AUTO else requested.engine_expression)
    tess_detail = tesseract.availability()
    pp = PPOCRv6MediumAdapter()
    pp_detail = pp.availability()
    return [
        {"engine_id": "tesseract_v2", "available": tess_detail.available, "reason": tess_detail.reason, "capabilities": tesseract.describe()["capabilities"]},
        {"engine_id": "ppocrv6_medium_v2", "available": pp_detail.available, "reason": pp_detail.reason, "capabilities": ["TEXT", "LINE_GEOMETRY"]},
    ]


@router.get("/capabilities")
async def capabilities(language: str = "eng") -> dict[str, object]:
    try:
        engines = _availability(language)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_LANGUAGE", "message": "Choose one or more installed OCR languages."}) from exc
    quality_available = any(
        engine["engine_id"] == "ppocrv6_medium_v2" and engine["available"]
        for engine in engines
    )
    tesseract_available = any(engine["engine_id"] == "tesseract_v2" and engine["available"] for engine in engines)
    languages = [
        {"code": code, "name": language_name(code)}
        for code in get_installed_tesseract_languages()
    ]
    return {
        "schema_version": "ocr_v2_capabilities.v1",
        "service_ready": True,
        "profile": "OCR_TEXT_V2",
        "languages": languages,
        "language_policy": {
            "modes": ["EXPLICIT", "AUTO"],
            "default_mode": "EXPLICIT",
            "max_languages": 3,
            "auto_statuses": ["DETECTED", "MULTILINGUAL_DETECTED", "UNCERTAIN", "UNDETERMINED"],
        },
        "routing_modes": [
            {"id": "AUTO", "label": "Balanced", "description": "Balances speed and extraction quality for the document.", "available": True},
            {"id": "FAST", "label": "Fast", "description": "Prioritizes a quicker result.", "available": True},
            {"id": "QUALITY", "label": "Best quality", "description": "Uses the highest-quality option when available.", "available": quality_available},
        ],
        "quality_engine_available": quality_available,
        "engines": engines,
        "profiles": ["OCR_TEXT_V2", "SEARCHABLE_PDF_V2"],
        "searchable_pdf": {
            "available": tesseract_available,
            "engine_id": "tesseract_v2",
            "required_capabilities": ["TEXT", "WORD_GEOMETRY", "READING_ORDER"],
            "input_formats": ["image/jpeg", "image/png", "image/webp"],
        },
    }


def _safe_error_code(failure_code: str | None) -> str:
    mapping = {
        "EngineUnavailableError": "ENGINE_UNAVAILABLE",
        "OCRTimeoutError": "TIMEOUT",
        "JobCancelledException": "CANCELLED",
        "NativeTextUndecidedError": "NATIVE_TEXT_UNDECIDED",
        "PageValidationError": "INVALID_ENGINE_OUTPUT",
        "LanguageDetectionUncertainError": "LANGUAGE_DETECTION_UNCERTAIN",
    }
    return mapping.get(failure_code or "", "ENGINE_FAILURE")


def _response(result: object, request: OCRV2WorkerRequest, warnings: list[str]) -> OCRV2WorkerResponse:
    pages = []
    for page in result.pages:
        source = page.provenance_refs[0] if page.provenance_refs else page.processing_source.value
        page_warnings = [warning for warning in warnings if warning.startswith("ENGINE_FALLBACK")]
        pages.append(OCRV2PageResponse(page_index=page.page_index, page_id=page.page_id, status=page.status.value, text=page.text, classification=page.content_classification.value, source=source, language={"requested": list(page.language.requested_languages), "detected": list(page.language.detected_languages), "status": page.language.language_status, "mode": page.language.requested_mode, "confidence": page.language.detection_confidence, "scripts": list(page.language.detected_scripts), "reason": page.language.detection_reason}, warning_codes=page_warnings))
    failed = next((page for page in result.pages if page.status.value == "FAILED"), None)
    status = "FAILED" if failed or not result.validation.valid else "SUCCEEDED"
    error = None
    if failed:
        error = OCRV2ErrorResponse(code=_safe_error_code(failed.failure_code), message="OCR V2 could not complete the requested document")
    elif not result.validation.valid:
        error = OCRV2ErrorResponse(code="PROFILE_NOT_ELIGIBLE", message="OCR V2 result did not satisfy the requested profile")
    return OCRV2WorkerResponse(request_id=request.request_id, profile=request.profile.value, status=status, text="\n\n".join(page.text.rstrip() for page in result.pages), pages=pages, warnings=warnings, error=error)


@router.post("/text", response_model=OCRV2WorkerResponse)
async def ocr_text_v2(
    request: Request,
    file: UploadFile = File(...),
    request_id: str = Form(...),
    profile: str = Form("OCR_TEXT_V2"),
    language: str | None = Form(None),
    language_mode: str = Form("EXPLICIT"),
    languages: list[str] | None = Form(None),
    routing_policy: str = Form("AUTO"),
) -> OCRV2WorkerResponse:
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Invalid request identifier"})
    try:
        contract = _request_model(request_id, profile, language, routing_policy, language_mode, languages)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "UNSUPPORTED_LANGUAGE":
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        raise
    path = await _stage_pdf(file)
    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)
    warnings: list[str] = []
    try:
        pp_available = PPOCRv6MediumAdapter().availability().available
        try:
            requested_policy = OCRLanguagePolicy.from_request(contract.language)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"code": "UNSUPPORTED_LANGUAGE", "message": "Choose one or more installed OCR languages."})
        tess_detail = TesseractAdapter("eng" if requested_policy.mode is OCRLanguageMode.AUTO else requested_policy.engine_expression).availability()
        if not pp_available and not tess_detail.available:
            return JSONResponse(status_code=422, content={"code": "UNSUPPORTED_LANGUAGE", "message": "The requested OCR language is not installed or supported by the available worker engines."})
        if contract.routing_policy in (OCRV2RoutingPolicy.AUTO, OCRV2RoutingPolicy.QUALITY, OCRV2RoutingPolicy.GEOMETRY) and not pp_available:
            warnings.append("ENGINE_FALLBACK:PP_OCR_UNAVAILABLE_TO_TESSERACT")
        try:
            result = await _run_worker(path, contract, check_cancel)
        except JobCancelledException as exc:
            raise HTTPException(status_code=499, detail={"code": "CANCELLED", "message": "OCR V2 request was cancelled"}) from exc
        except OCRTextEngineConfigurationError as exc:
            raise HTTPException(status_code=500, detail={"code": "INVALID_CONFIGURATION", "message": "OCR Text V2 engine configuration is invalid"}) from exc
        except OCRTextEngineUnavailableError:
            return JSONResponse(status_code=503, content={"code": "ENGINE_UNAVAILABLE", "message": "OCR Text V2 engine is unavailable"})
        response = _response(result, contract, warnings)
        if response.status == "FAILED":
            return JSONResponse(status_code=_failure_http_status(response.error.code if response.error else "ENGINE_FAILURE"), content=response.model_dump())
        return response
    finally:
        cleanup_cancel()
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/markup/preview", response_model=OCRV2MarkupPreviewResponse)
async def markup_preview(
    request: Request,
    file: UploadFile = File(...),
    request_id: str = Form(...),
    profile: str = Form("MARKUP_V2"),
    language: str | None = Form(None),
    language_mode: str = Form("EXPLICIT"),
    languages: list[str] | None = Form(None),
    routing_policy: str = Form("FAST"),
) -> OCRV2MarkupPreviewResponse:
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Invalid request identifier"})
    try:
        contract = _markup_preview_request_model(request_id, profile, language, routing_policy, language_mode, languages)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "UNSUPPORTED_LANGUAGE":
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        raise
    path = await _stage_pdf(file)
    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)
    try:
        result = await _run_markup_preview(path, contract, check_cancel)
        return OCRV2MarkupPreviewResponse(**result)
    except JobCancelledException as exc:
        raise HTTPException(status_code=499, detail={"code": "CANCELLED", "message": "Selectable text preparation was cancelled"}) from exc
    except OcrMarkupEngineConfigurationError as exc:
        raise HTTPException(status_code=500, detail={"code": "INVALID_CONFIGURATION", "message": "Markup preview configuration is invalid"}) from exc
    except (OcrMarkupEngineUnavailableError, EngineUnavailableError):
        return JSONResponse(status_code=503, content={"code": "ENGINE_UNAVAILABLE", "message": "Selectable text is temporarily unavailable"})
    except LanguageDetectionUncertainError:
        return JSONResponse(status_code=422, content={"code": "LANGUAGE_DETECTION_UNCERTAIN", "message": "The document language could not be determined reliably"})
    except OCRTimeoutError:
        return JSONResponse(status_code=504, content={"code": "TIMEOUT", "message": "Preparing selectable text took too long"})
    except WordGeometryUnavailableError:
        return JSONResponse(status_code=422, content={"code": "WORD_GEOMETRY_NOT_AVAILABLE", "message": "Selectable text is not available for this document"})
    except Exception:
        return JSONResponse(status_code=502, content={"code": "ENGINE_FAILURE", "message": "Selectable text could not be prepared"})
    finally:
        cleanup_cancel()
        try:
            os.remove(path)
        except OSError:
            pass


async def _run_worker(path: str, contract: OCRV2WorkerRequest, check_cancel: object) -> object:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        execute_ocr_text,
        path,
        language=contract.language,
        language_mode=contract.language_mode.value,
        languages=contract.languages,
        language_usage=contract.language_usage,
        routing_policy=contract.routing_policy.value,
        cancellation_check=check_cancel,
    )


async def _run_markup_preview(path: str, contract: OCRV2MarkupPreviewRequest, check_cancel: object) -> object:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        execute_ocr_markup_preview,
        path,
        language=contract.language,
        language_mode=contract.language_mode.value,
        languages=contract.languages,
        language_usage=contract.language_usage,
        cancellation_check=check_cancel,
    )


def _failure_http_status(code: str) -> int:
    if code == "ENGINE_UNAVAILABLE":
        return 503
    if code == "TIMEOUT":
        return 504
    if code in {"PROFILE_NOT_ELIGIBLE", "INVALID_ENGINE_OUTPUT"}:
        return 422
    return 502

"""Controlled Searchable PDF V2 engine selection for PDFNest.

The Searchable PDF consumer owns the application lifecycle around storage,
normalization, durable jobs, and persistence. This module owns only the engine
boundary: the frozen PDFNest implementation remains the default, while the
standalone SDK is an explicit opt-in. Both branches execute one OCR pass and
produce the same canonical result before the application marks the job done.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence

from app.core.ocr_v2.errors import (
    EngineUnavailableError,
    OCRTimeoutError,
    RenderingNotEligibleError,
)


SEARCHABLE_PDF_ENGINE_ENV = "SEARCHABLE_PDF_ENGINE"
DEFAULT_SEARCHABLE_PDF_ENGINE = "internal"
SUPPORTED_SEARCHABLE_PDF_ENGINES = frozenset({"internal", "sdk"})

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]
AfterOcrCallback = Callable[[object], None]


class SearchablePdfEngineConfigurationError(ValueError):
    """The Searchable PDF engine selector is missing or unsupported."""


class SearchablePdfEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected Searchable PDF engine cannot be loaded."""


def configured_searchable_pdf_engine(raw: str | None = None) -> str:
    """Return the normalized Searchable PDF engine selector.

    Blank or absent configuration intentionally selects the frozen internal
    engine. A non-blank unsupported value is rejected; there is no implicit
    SDK-to-internal fallback that could hide a migration failure.
    """

    value = os.getenv(SEARCHABLE_PDF_ENGINE_ENV, DEFAULT_SEARCHABLE_PDF_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_SEARCHABLE_PDF_ENGINE
    if normalized not in SUPPORTED_SEARCHABLE_PDF_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_SEARCHABLE_PDF_ENGINES))
        raise SearchablePdfEngineConfigurationError(
            f"{SEARCHABLE_PDF_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_worker() -> Any:
    """Construct the frozen worker with the existing geometry-safe route."""

    from app.core.ocr_v2 import OCRV2Worker
    from app.core.ocr_v2.routing import RoutePolicy

    # Searchable PDF requires genuine word geometry. Preserve the existing
    # product behavior: this profile uses the Tesseract word-level path rather
    # than a line-only alternative, regardless of the product's general route
    # preference.
    return OCRV2Worker(
        route_policy=RoutePolicy(
            preferred_engine="tesseract_v2",
            fallback_engine="tesseract_v2",
        )
    )


def _internal_renderer() -> Any:
    from app.core.ocr_v2.renderers.searchable_pdf import SearchablePdfRenderer

    return SearchablePdfRenderer()


def _sdk_processor() -> Any:
    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise SearchablePdfEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor()


def _sdk_profile() -> Any:
    """Load the public SDK profile enum without importing SDK implementation modules."""

    from platen_document import OCRProfile

    return OCRProfile.SEARCHABLE_PDF_V2


def _raise_sdk_exception(exc: Exception) -> NoReturn:
    """Translate public SDK typed failures into the worker boundary contract."""

    name = type(exc).__name__
    if name == "RenderingNotEligibleError":
        raise RenderingNotEligibleError(
            str(exc) or "searchable PDF rendering was not eligible",
            substage=getattr(exc, "substage", None),
            reason_code=getattr(exc, "reason_code", None),
        ) from exc
    if name == "EngineUnavailableError":
        raise EngineUnavailableError(
            str(exc) or "OCR engine was unavailable while creating a searchable PDF"
        ) from exc
    if name == "OCRTimeoutError":
        raise OCRTimeoutError(str(exc) or "searchable PDF OCR timed out") from exc
    raise exc


def _execute_internal(
    source_pdf: str | Path,
    output_pdf: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
    after_ocr: AfterOcrCallback | None,
    diagnostic_job_id: str | None,
) -> Any:
    from app.core.ocr_v2.validation import OCRProfile

    result = _internal_worker().process_document(
        source_pdf,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        profile=OCRProfile.SEARCHABLE_PDF_V2,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )
    if after_ocr is not None:
        after_ocr(result)
    _internal_renderer().render(source_pdf, result, output_pdf, job_id=diagnostic_job_id)
    return result


def _execute_sdk(
    source_pdf: str | Path,
    output_pdf: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
    after_ocr: AfterOcrCallback | None,
    diagnostic_job_id: str | None,
) -> Any:
    processor = _sdk_processor()
    try:
        # FAST maps to the SDK's Tesseract-only route, matching the existing
        # internal Searchable PDF route and preserving word-geometry parity.
        result = processor.extract_text(
            source_pdf,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            profile=_sdk_profile(),
            routing_policy="FAST",
            cancellation_check=cancellation_check,
            page_progress_callback=page_progress_callback,
        )
    except Exception as exc:
        _raise_sdk_exception(exc)

    # The callback is deliberately outside SDK exception translation. It is
    # application-owned validation/progress logic and must retain its existing
    # cancellation and failure types.
    if after_ocr is not None:
        after_ocr(result)

    try:
        # Supplying the already extracted result is essential: this is a
        # render-only second call, not a second OCR pass.
        processor.make_searchable_pdf(
            source_pdf,
            output_pdf,
            result=result,
            job_id=diagnostic_job_id,
        )
    except Exception as exc:
        _raise_sdk_exception(exc)
    return result


def execute_searchable_pdf(
    source_pdf: str | Path,
    output_pdf: str | Path,
    *,
    language: str,
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
    after_ocr: AfterOcrCallback | None = None,
    diagnostic_job_id: str | None = None,
) -> Any:
    """Run the selected Searchable PDF engine through one controlled boundary."""

    selected = configured_searchable_pdf_engine()
    logger.info("OCR_V2_SEARCHABLE_ENGINE consumer=searchable_pdf engine=%s", selected)
    if selected == "internal":
        return _execute_internal(
            source_pdf,
            output_pdf,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            cancellation_check=cancellation_check,
            page_progress_callback=page_progress_callback,
            after_ocr=after_ocr,
            diagnostic_job_id=diagnostic_job_id,
        )
    return _execute_sdk(
        source_pdf,
        output_pdf,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
        after_ocr=after_ocr,
        diagnostic_job_id=diagnostic_job_id,
    )


__all__ = [
    "DEFAULT_SEARCHABLE_PDF_ENGINE",
    "SEARCHABLE_PDF_ENGINE_ENV",
    "SUPPORTED_SEARCHABLE_PDF_ENGINES",
    "SearchablePdfEngineConfigurationError",
    "SearchablePdfEngineUnavailableError",
    "configured_searchable_pdf_engine",
    "execute_searchable_pdf",
]

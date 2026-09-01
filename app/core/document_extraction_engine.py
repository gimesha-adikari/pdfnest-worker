"""Controlled Document Extraction V2 engine selection for PDFNest.

The durable actor remains responsible for PDFNest storage, jobs, ownership,
progress persistence, and result publication.  This module owns only the
consumer boundary between the frozen structured implementation and the
standalone ``platen_document`` SDK.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DOCUMENT_EXTRACTION_ENGINE_ENV = "DOCUMENT_EXTRACTION_ENGINE"
DEFAULT_DOCUMENT_EXTRACTION_ENGINE = "internal"
SUPPORTED_DOCUMENT_EXTRACTION_ENGINES = frozenset({"internal", "sdk"})

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]


class DocumentExtractionEngineConfigurationError(ValueError):
    """The Document Extraction engine selector is missing or unsupported."""


class DocumentExtractionEngineUnavailableError(RuntimeError):
    """The explicitly selected Document Extraction engine cannot be loaded."""


def configured_document_extraction_engine(raw: str | None = None) -> str:
    """Return the normalized selector with no implicit runtime fallback."""

    value = os.getenv(DOCUMENT_EXTRACTION_ENGINE_ENV, DEFAULT_DOCUMENT_EXTRACTION_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_DOCUMENT_EXTRACTION_ENGINE
    if normalized not in SUPPORTED_DOCUMENT_EXTRACTION_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_DOCUMENT_EXTRACTION_ENGINES))
        raise DocumentExtractionEngineConfigurationError(
            f"{DOCUMENT_EXTRACTION_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_processor() -> Any:
    """Construct the frozen internal structured processor."""

    from app.core.ocr_v2.structured import StructuredDocumentProcessor

    return StructuredDocumentProcessor()


def _sdk_processor() -> Any:
    """Construct the public standalone SDK processor only in SDK mode."""

    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise DocumentExtractionEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor()


def _execute_internal(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    routing_policy: str | object | None,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    return _internal_processor().process_document(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )


def _execute_sdk(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    routing_policy: str | object | None,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    return _sdk_processor().extract_document(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )


def execute_document_extraction(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    routing_policy: str | object | None = "AUTO",
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> Any:
    """Execute Document Extraction through the explicitly selected engine."""

    selected = configured_document_extraction_engine()
    logger.info("OCR_V2_DOCUMENT_EXTRACTION_ENGINE consumer=document_extraction engine=%s", selected)
    executor = _execute_internal if selected == "internal" else _execute_sdk
    return executor(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )


__all__ = [
    "DEFAULT_DOCUMENT_EXTRACTION_ENGINE",
    "DOCUMENT_EXTRACTION_ENGINE_ENV",
    "SUPPORTED_DOCUMENT_EXTRACTION_ENGINES",
    "DocumentExtractionEngineConfigurationError",
    "DocumentExtractionEngineUnavailableError",
    "configured_document_extraction_engine",
    "execute_document_extraction",
]

"""Controlled PDF-to-Word OCR fallback engine selection.

The PDF-to-Word product keeps its native ``pdf2docx`` path and its DOCX
projection in PDFNest.  This module owns only the document-processing boundary
used when native text is not trusted.  The frozen internal structured
processor remains the default; the standalone SDK is an explicit opt-in.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


PDF_TO_WORD_OCR_ENGINE_ENV = "PDF_TO_WORD_OCR_ENGINE"
DEFAULT_PDF_TO_WORD_OCR_ENGINE = "internal"
SUPPORTED_PDF_TO_WORD_OCR_ENGINES = frozenset({"internal", "sdk"})

logger = logging.getLogger(__name__)


class PdfToWordOcrEngineConfigurationError(ValueError):
    """The PDF-to-Word OCR fallback selector is unsupported."""


class PdfToWordOcrEngineUnavailableError(RuntimeError):
    """The explicitly selected PDF-to-Word OCR engine cannot be loaded."""


class PdfToWordOcrEngineExecutionError(RuntimeError):
    """The explicitly selected PDF-to-Word OCR engine failed during execution."""


def configured_pdf_to_word_ocr_engine(raw: str | None = None) -> str:
    """Return the normalized selector without an implicit runtime fallback."""

    value = (
        os.getenv(PDF_TO_WORD_OCR_ENGINE_ENV, DEFAULT_PDF_TO_WORD_OCR_ENGINE)
        if raw is None
        else raw
    )
    normalized = str(value).strip().lower() or DEFAULT_PDF_TO_WORD_OCR_ENGINE
    if normalized not in SUPPORTED_PDF_TO_WORD_OCR_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_PDF_TO_WORD_OCR_ENGINES))
        raise PdfToWordOcrEngineConfigurationError(
            f"{PDF_TO_WORD_OCR_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_processor() -> Any:
    """Construct the frozen internal structured processor only in internal mode."""

    from app.core.ocr_v2.structured import StructuredDocumentProcessor

    return StructuredDocumentProcessor()


def _sdk_processor() -> Any:
    """Construct the public standalone SDK processor only in SDK mode."""

    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise PdfToWordOcrEngineUnavailableError(
                "the selected PDF-to-Word OCR engine is unavailable"
            ) from exc
        raise
    return DocumentProcessor()


def _execute_internal(pdf_path: str | Path, *, language: str) -> Any:
    return _internal_processor().process_document(pdf_path, language=language)


def _execute_sdk(pdf_path: str | Path, *, language: str) -> Any:
    # ``extract_document`` is the public SDK contract for the canonical
    # structured result.  The result is passed directly to the existing
    # PDFNest-owned DOCX projection; there is no second extraction pass.
    try:
        return _sdk_processor().extract_document(pdf_path, language=language)
    except PdfToWordOcrEngineUnavailableError:
        raise
    except Exception as exc:
        # The worker service currently projects converter exceptions into the
        # HTTP response. Keep the selected SDK failure observable as a failure,
        # but do not leak SDK package names, local paths, or raw trace details.
        raise PdfToWordOcrEngineExecutionError(
            "PDF-to-Word OCR processing failed"
        ) from exc


def execute_pdf_to_word_ocr(pdf_path: str | Path, *, language: str = "eng") -> Any:
    """Extract the canonical document result for the OCR fallback."""

    selected = configured_pdf_to_word_ocr_engine()
    logger.info(
        "OCR_V2_PDF_TO_WORD_OCR_ENGINE consumer=pdf_to_word engine=%s",
        selected,
    )
    executor = _execute_internal if selected == "internal" else _execute_sdk
    return executor(pdf_path, language=language)


__all__ = [
    "DEFAULT_PDF_TO_WORD_OCR_ENGINE",
    "PDF_TO_WORD_OCR_ENGINE_ENV",
    "PdfToWordOcrEngineConfigurationError",
    "PdfToWordOcrEngineExecutionError",
    "PdfToWordOcrEngineUnavailableError",
    "SUPPORTED_PDF_TO_WORD_OCR_ENGINES",
    "configured_pdf_to_word_ocr_engine",
    "execute_pdf_to_word_ocr",
]

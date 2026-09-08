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
from typing import Any, Callable, Sequence


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


class _PageScopedRasterPreparer:
    """Select one cached raster preparer for each PDF page's target DPI."""

    def __init__(self, raster_dpis: Sequence[int], factory: Callable[[int], Any]) -> None:
        normalized = tuple(int(dpi) for dpi in raster_dpis)
        if not normalized or any(dpi <= 0 for dpi in normalized):
            raise ValueError("raster_dpis must contain only positive DPI values")
        self.raster_dpis = normalized
        self._factory = factory
        self._preparers: dict[int, Any] = {}

    def prepare(self, page: Any) -> Any:
        page_number = getattr(page, "number", None)
        if page_number is None:
            raise ValueError("PDF page does not expose a zero-based page number")
        page_index = int(page_number)
        if page_index < 0 or page_index >= len(self.raster_dpis):
            raise ValueError(f"no PDF-to-Word raster DPI is configured for page {page_index}")
        dpi = self.raster_dpis[page_index]
        preparer = self._preparers.get(dpi)
        if preparer is None:
            preparer = self._factory(dpi)
            self._preparers[dpi] = preparer
        return preparer.prepare(page)


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


def _internal_processor(
    *,
    raster_dpi: int | None = None,
    raster_dpis: Sequence[int] | None = None,
) -> Any:
    """Construct the frozen internal structured processor only in internal mode."""

    from app.core.ocr_v2.structured import StructuredDocumentProcessor
    from app.core.ocr_v2.geometry import RasterPreparer

    if raster_dpi is not None and raster_dpis is not None:
        raise ValueError("raster_dpi and raster_dpis are mutually exclusive")
    if raster_dpis is not None:
        return StructuredDocumentProcessor(
            raster_preparer=_PageScopedRasterPreparer(raster_dpis, RasterPreparer)
        )
    if raster_dpi is None:
        return StructuredDocumentProcessor()
    return StructuredDocumentProcessor(raster_dpi=raster_dpi)


def _sdk_processor(
    *,
    raster_dpi: int | None = None,
    raster_dpis: Sequence[int] | None = None,
) -> Any:
    """Construct the public standalone SDK processor only in SDK mode."""

    if raster_dpi is not None and raster_dpis is not None:
        raise ValueError("raster_dpi and raster_dpis are mutually exclusive")
    try:
        from platen_document import DocumentProcessor
        if raster_dpi is not None:
            from platen_document import EngineConfiguration
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise PdfToWordOcrEngineUnavailableError(
                "the selected PDF-to-Word OCR engine is unavailable"
            ) from exc
        raise
    if raster_dpi is not None:
        return DocumentProcessor(EngineConfiguration(raster_dpi=raster_dpi))
    processor = DocumentProcessor()
    if raster_dpis is None:
        return processor

    # SDK 0.1.2 exposes one OCR worker inside the public DocumentProcessor,
    # but its public constructor accepts only one document-wide raster DPI.
    # Keep the SDK immutable and replace only that worker's preparer with a
    # page-aware adapter; extract_document still runs exactly once.
    try:
        ocr_worker = processor._structured_processor.ocr_worker
        base_preparer = ocr_worker.raster_preparer
        metadata_policy = base_preparer.dpi_metadata_policy
        preparer_type = type(base_preparer)
        ocr_worker.raster_preparer = _PageScopedRasterPreparer(
            raster_dpis,
            lambda dpi: preparer_type(dpi, dpi_metadata_policy=metadata_policy),
        )
    except AttributeError as exc:
        raise PdfToWordOcrEngineUnavailableError(
            "the selected PDF-to-Word OCR engine cannot apply page-scoped rasterization"
        ) from exc
    return processor


def _execute_internal(
    pdf_path: str | Path,
    *,
    language: str,
    raster_dpi: int | None = None,
    raster_dpis: Sequence[int] | None = None,
) -> Any:
    if raster_dpi is not None and raster_dpis is not None:
        raise ValueError("raster_dpi and raster_dpis are mutually exclusive")
    if raster_dpis is not None:
        processor = _internal_processor(raster_dpis=raster_dpis)
    elif raster_dpi is None:
        processor = _internal_processor()
    else:
        processor = _internal_processor(raster_dpi=raster_dpi)
    return processor.process_document(pdf_path, language=language)


def _execute_sdk(
    pdf_path: str | Path,
    *,
    language: str,
    raster_dpi: int | None = None,
    raster_dpis: Sequence[int] | None = None,
) -> Any:
    # ``extract_document`` is the public SDK contract for the canonical
    # structured result.  The result is passed directly to the existing
    # PDFNest-owned DOCX projection; there is no second extraction pass.
    try:
        if raster_dpi is not None and raster_dpis is not None:
            raise ValueError("raster_dpi and raster_dpis are mutually exclusive")
        if raster_dpis is not None:
            processor = _sdk_processor(raster_dpis=raster_dpis)
        elif raster_dpi is None:
            processor = _sdk_processor()
        else:
            processor = _sdk_processor(raster_dpi=raster_dpi)
        return processor.extract_document(pdf_path, language=language)
    except PdfToWordOcrEngineUnavailableError:
        raise
    except Exception as exc:
        # The worker service currently projects converter exceptions into the
        # HTTP response. Keep the selected SDK failure observable as a failure,
        # but do not leak SDK package names, local paths, or raw trace details.
        raise PdfToWordOcrEngineExecutionError(
            "PDF-to-Word OCR processing failed"
        ) from exc


def execute_pdf_to_word_ocr(
    pdf_path: str | Path,
    *,
    language: str = "eng",
    raster_dpi: int | None = None,
    raster_dpis: Sequence[int] | None = None,
) -> Any:
    """Extract the canonical document result for the OCR fallback."""

    selected = configured_pdf_to_word_ocr_engine()
    logger.info(
        "OCR_V2_PDF_TO_WORD_OCR_ENGINE consumer=pdf_to_word engine=%s raster_dpi=%s raster_dpis=%s",
        selected,
        raster_dpi if raster_dpi is not None else "default",
        tuple(raster_dpis) if raster_dpis is not None else "default",
    )
    executor = _execute_internal if selected == "internal" else _execute_sdk
    return executor(
        pdf_path,
        language=language,
        raster_dpi=raster_dpi,
        raster_dpis=raster_dpis,
    )


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

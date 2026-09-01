"""Controlled PDF-to-Markdown V2 engine selection for PDFNest.

The durable structured actor remains responsible for PDFNest storage, jobs,
ownership, progress, cancellation, and result persistence.  This module owns
only the PDF-to-Markdown consumer boundary.  The internal structured processor
and Markdown renderer remain the default; the standalone SDK is an explicit
opt-in.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PDF_TO_MARKDOWN_ENGINE_ENV = "PDF_TO_MARKDOWN_ENGINE"
DEFAULT_PDF_TO_MARKDOWN_ENGINE = "internal"
SUPPORTED_PDF_TO_MARKDOWN_ENGINES = frozenset({"internal", "sdk"})

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]


class PdfToMarkdownEngineConfigurationError(ValueError):
    """The PDF-to-Markdown engine selector is missing or unsupported."""


class PdfToMarkdownEngineUnavailableError(RuntimeError):
    """The explicitly selected PDF-to-Markdown engine cannot be loaded."""


@dataclass(frozen=True)
class PdfToMarkdownExecution:
    """The canonical structured result and its one-pass Markdown projection."""

    structured_result: Any
    markdown: str


def configured_pdf_to_markdown_engine(raw: str | None = None) -> str:
    """Return the normalized selector without an implicit runtime fallback."""

    value = os.getenv(PDF_TO_MARKDOWN_ENGINE_ENV, DEFAULT_PDF_TO_MARKDOWN_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_PDF_TO_MARKDOWN_ENGINE
    if normalized not in SUPPORTED_PDF_TO_MARKDOWN_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_PDF_TO_MARKDOWN_ENGINES))
        raise PdfToMarkdownEngineConfigurationError(
            f"{PDF_TO_MARKDOWN_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_processor() -> Any:
    """Construct the frozen internal structured processor only in internal mode."""

    from app.core.ocr_v2.structured import StructuredDocumentProcessor

    return StructuredDocumentProcessor()


def _internal_markdown(result: Any) -> str:
    """Render with the frozen internal Markdown implementation."""

    from app.core.ocr_v2.structured import render_structured_markdown

    return render_structured_markdown(result)


def _sdk_processor() -> Any:
    """Construct the public standalone SDK processor only in SDK mode."""

    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise PdfToMarkdownEngineUnavailableError(
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
) -> PdfToMarkdownExecution:
    result = _internal_processor().process_document(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )
    return PdfToMarkdownExecution(structured_result=result, markdown=_internal_markdown(result))


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
) -> PdfToMarkdownExecution:
    processor = _sdk_processor()
    # Extract once, then render the already-produced canonical result.  Calling
    # to_markdown(result) is intentionally render-only and cannot trigger a
    # second OCR/structured pass.
    result = processor.extract_document(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )
    markdown = processor.to_markdown(result, emit_page_breaks=True)
    return PdfToMarkdownExecution(structured_result=result, markdown=markdown)


def execute_pdf_to_markdown(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    routing_policy: str | object | None = "AUTO",
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> PdfToMarkdownExecution:
    """Run PDF-to-Markdown through the explicitly selected implementation."""

    selected = configured_pdf_to_markdown_engine()
    logger.info("OCR_V2_PDF_TO_MARKDOWN_ENGINE consumer=pdf_to_markdown engine=%s", selected)
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
    "DEFAULT_PDF_TO_MARKDOWN_ENGINE",
    "PDF_TO_MARKDOWN_ENGINE_ENV",
    "SUPPORTED_PDF_TO_MARKDOWN_ENGINES",
    "PdfToMarkdownEngineConfigurationError",
    "PdfToMarkdownEngineUnavailableError",
    "PdfToMarkdownExecution",
    "configured_pdf_to_markdown_engine",
    "execute_pdf_to_markdown",
]

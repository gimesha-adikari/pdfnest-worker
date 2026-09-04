"""Controlled engine selection for the standalone OCR-aware markup products.

The durable PDFNest actor remains responsible for input storage, ownership,
progress, cancellation, and output persistence.  This module only selects
the implementation used by the three standalone V2 markup products:
Highlight, Underline, and Strikeout.

The shared low-level markup helper is intentionally loaded lazily in the
internal branch.  That keeps the SDK branch above the shared helper used by
Studio and prevents a selected SDK failure from silently falling back to the
frozen internal implementation.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pymupdf as fitz

from app.core.ocr_v2.errors import (
    AnnotationWriteError,
    EngineUnavailableError,
    LanguageDetectionUncertainError,
    MarkupError,
    OCRTimeoutError,
    TextNotFoundError,
    WordGeometryUnavailableError,
)
from app.core.ocr_markup_preview import project_markup_preview


OCR_MARKUP_ENGINE_ENV = "OCR_MARKUP_ENGINE"
DEFAULT_OCR_MARKUP_ENGINE = "internal"
SUPPORTED_OCR_MARKUP_ENGINES = frozenset({"internal", "sdk"})

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
ProgressCallback = Callable[[int, int], None]


class OcrMarkupEngineConfigurationError(ValueError):
    """The OCR-aware markup engine selector is unsupported."""


class OcrMarkupEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected OCR-aware markup engine cannot be loaded."""


@dataclass(frozen=True)
class ExplicitMarkupExecutionResult:
    action: str
    mode: str
    page_count: int
    selection: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        page_index = int(self.selection["page"]) - 1
        rects = list(self.selection["rects"])
        return {
            "schema_version": "ocr_v2_markup_result.v1",
            "action": self.action,
            "mode": self.mode,
            "source_policy": "BROWSER_SELECTION_CANONICAL_PDF_POINTS",
            "page_count": self.page_count,
            "selection_count": 1,
            "selections": [{
                "page_index": page_index,
                "matched_text": str(self.selection.get("text", "")),
                "word_ids": list(self.selection.get("word_ids", [])),
                "group_rects": rects,
                "source_type": str(self.selection.get("source", "")),
                "provenance": ["browser-selection"],
            }],
            "page_sources": [{
                "page_index": page_index,
                "source_type": str(self.selection.get("source", "")),
                "word_geometry": True,
                "reading_order": bool(self.selection.get("word_ids")),
            }],
        }


def configured_ocr_markup_engine(raw: str | None = None) -> str:
    """Return the normalized selector with the internal path as the default."""

    value = os.getenv(OCR_MARKUP_ENGINE_ENV, DEFAULT_OCR_MARKUP_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_OCR_MARKUP_ENGINE
    if normalized not in SUPPORTED_OCR_MARKUP_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_OCR_MARKUP_ENGINES))
        raise OcrMarkupEngineConfigurationError(
            f"{OCR_MARKUP_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _enum_value(value: str | object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def _execute_internal(
    input_path: str | Path,
    output_path: str | Path,
    *,
    action: str | object,
    query: str,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    mode: str | object,
    color: tuple[float, float, float],
    cancellation_check: CancellationCheck | None,
    progress_callback: ProgressCallback | None,
) -> Any:
    """Call the unchanged PDFNest markup implementation only in internal mode."""

    from app.core.ocr_v2.markup import MarkupAction, MarkupMode, apply_ocr_markup

    return apply_ocr_markup(
        input_path,
        output_path,
        action=MarkupAction(_enum_value(action)),
        query=query,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        mode=MarkupMode(_enum_value(mode)),
        color=color,
        cancellation_check=cancellation_check,
        progress_callback=progress_callback,
    )


def _sdk_processor() -> Any:
    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise OcrMarkupEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor()


def _translate_sdk_error(exc: Exception) -> None:
    """Map public SDK markup failures onto PDFNest's existing safe contract."""

    try:
        from platen_document import (
            AnnotationWriteError as SdkAnnotationWriteError,
            AmbiguousSelectionError as SdkAmbiguousSelectionError,
            EngineUnavailableError as SdkEngineUnavailableError,
            MarkupError as SdkMarkupError,
            OCRTimeoutError as SdkOCRTimeoutError,
            TextNotFoundError as SdkTextNotFoundError,
            WordGeometryUnavailableError as SdkWordGeometryUnavailableError,
        )
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise OcrMarkupEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise

    if isinstance(exc, SdkTextNotFoundError):
        raise TextNotFoundError(str(exc)) from exc
    if isinstance(exc, SdkWordGeometryUnavailableError):
        raise WordGeometryUnavailableError(str(exc)) from exc
    if isinstance(exc, SdkAnnotationWriteError):
        raise AnnotationWriteError(str(exc)) from exc
    if isinstance(exc, SdkOCRTimeoutError):
        raise OCRTimeoutError(str(exc)) from exc
    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError(str(exc)) from exc
    if isinstance(exc, (SdkAmbiguousSelectionError, SdkMarkupError)):
        raise MarkupError(str(exc)) from exc


def _execute_sdk(
    input_path: str | Path,
    output_path: str | Path,
    *,
    action: str | object,
    query: str,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    mode: str | object,
    color: tuple[float, float, float],
    cancellation_check: CancellationCheck | None,
    progress_callback: ProgressCallback | None,
) -> Any:
    """Call only the public standalone SDK markup API in SDK mode."""

    processor = _sdk_processor()
    try:
        return processor.apply_markup(
            input_path,
            output_path,
            action=_enum_value(action),
            query=query,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            mode=_enum_value(mode),
            color=color,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        _translate_sdk_error(exc)
        raise


def execute_ocr_markup(
    input_path: str | Path,
    output_path: str | Path,
    *,
    action: str | object,
    query: str = "",
    selection: Mapping[str, Any] | None = None,
    language: str = "eng",
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    mode: str | object = "smart",
    color: tuple[float, float, float] = (1.0, 1.0, 0.0),
    cancellation_check: CancellationCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Any:
    """Execute standalone V2 Highlight, Underline, or Strikeout."""

    if selection is not None:
        return execute_ocr_markup_selection(
            input_path,
            output_path,
            action=action,
            mode=mode,
            selection=selection,
            color=color,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
        )

    selected = configured_ocr_markup_engine()
    logger.info("OCR_V2_MARKUP_ENGINE consumer=ocr_markup engine=%s", selected)
    executor = _execute_internal if selected == "internal" else _execute_sdk
    return executor(
        input_path,
        output_path,
        action=action,
        query=query,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        mode=mode,
        color=color,
        cancellation_check=cancellation_check,
        progress_callback=progress_callback,
    )


def _explicit_selection_rects(selection: Mapping[str, Any], page: Any) -> list[Any]:
    coordinate_space = str(selection.get("coordinate_space", ""))
    if coordinate_space != "pdf_points_visible_cropbox_top_left":
        raise ValueError("unsupported browser selection coordinate space")
    page_width = float(selection.get("page_width", 0))
    page_height = float(selection.get("page_height", 0))
    if not math.isfinite(page_width) or not math.isfinite(page_height) or page_width <= 0 or page_height <= 0:
        raise ValueError("browser selection page geometry is invalid")
    if abs(page.rect.width - page_width) > 0.5 or abs(page.rect.height - page_height) > 0.5:
        raise ValueError("browser selection page geometry does not match the PDF page")

    raw_rects = selection.get("rects")
    if not isinstance(raw_rects, (list, tuple)) or not raw_rects:
        raise ValueError("browser selection has no rectangles")
    rects: list[Any] = []
    for raw_rect in raw_rects:
        if not isinstance(raw_rect, Mapping):
            raise ValueError("browser selection rectangle is invalid")
        x = float(raw_rect.get("x", 0))
        y = float(raw_rect.get("y", 0))
        width = float(raw_rect.get("width", 0))
        height = float(raw_rect.get("height", 0))
        if not all(math.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0 or x < -0.5 or y < -0.5 or x + width > page_width + 0.5 or y + height > page_height + 0.5:
            raise ValueError("browser selection rectangle is outside the PDF page")
        left = max(0.0, min(page_width, x))
        top = max(0.0, min(page_height, y))
        right = max(left, min(page_width, x + width))
        bottom = max(top, min(page_height, y + height))
        if right <= left or bottom <= top:
            raise ValueError("browser selection rectangle is empty")
        rects.append(fitz.Rect(left, top, right, bottom))
    return rects


def execute_ocr_markup_selection(
    input_path: str | Path,
    output_path: str | Path,
    *,
    action: str | object,
    mode: str | object = "smart",
    selection: Mapping[str, Any],
    color: tuple[float, float, float] = (1.0, 1.0, 0.0),
    cancellation_check: CancellationCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ExplicitMarkupExecutionResult:
    """Apply already-normalized browser selection geometry without OCR."""

    action_value = _enum_value(action)
    if action_value not in {"highlight", "underline", "strikeout"}:
        raise ValueError("unsupported markup action")
    page_number = int(selection.get("page", 0))
    if page_number < 1:
        raise ValueError("browser selection page is invalid")
    if str(selection.get("source", "")) not in {"native", "ocr"}:
        raise ValueError("browser selection source is invalid")
    if cancellation_check:
        cancellation_check()

    with fitz.open(str(input_path)) as document:
        if page_number > len(document):
            raise ValueError("browser selection page is unavailable")
        page = document[page_number - 1]
        quads = _explicit_selection_rects(selection, page)
        if action_value == "highlight":
            annotation = page.add_highlight_annot(quads)
        elif action_value == "underline":
            annotation = page.add_underline_annot(quads)
        else:
            annotation = page.add_strikeout_annot(quads)
        if annotation is None:
            raise AnnotationWriteError(f"could not write {action_value} annotation")
        annotation.set_colors(stroke=color)
        annotation.update()
        if progress_callback:
            progress_callback(page_number, len(document))
        if cancellation_check:
            cancellation_check()
        document.save(str(output_path), garbage=4, deflate=True)
        page_count = len(document)

    return ExplicitMarkupExecutionResult(
        action=action_value,
        mode=_enum_value(mode),
        page_count=page_count,
        selection={
            **dict(selection),
            "page": page_number,
            "rects": [
                {"x": rect.x0, "y": rect.y0, "width": rect.width, "height": rect.height}
                for rect in quads
            ],
        },
    )


def _preview_internal(
    input_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    cancellation_check: CancellationCheck | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Build a preview from the unchanged internal canonical OCR pipeline."""

    from app.core.ocr_v2.orchestration import OCRV2Worker
    from app.core.ocr_v2.routing import RoutePolicy
    from app.core.ocr_v2.validation import OCRProfile

    worker = OCRV2Worker(route_policy=RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2"))
    result = worker.process_document(
        input_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        profile=OCRProfile.OCR_TEXT_V2,
        cancellation_check=cancellation_check,
        page_progress_callback=lambda done, total, _page: progress_callback(done, total) if progress_callback else None,
    )
    failed = next((page for page in result.pages if page.status.value == "FAILED"), None)
    if failed:
        if failed.failure_code == "LanguageDetectionUncertainError":
            raise LanguageDetectionUncertainError("the document language could not be determined reliably")
        if failed.failure_code == "EngineUnavailableError":
            raise EngineUnavailableError("OCR engine was unavailable while preparing selectable text")
        if failed.failure_code == "OCRTimeoutError":
            raise OCRTimeoutError("OCR exceeded the markup preview deadline")
        raise WordGeometryUnavailableError("a page did not produce selectable word geometry")
    return project_markup_preview(result, input_path)


def _preview_sdk(
    input_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    cancellation_check: CancellationCheck | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Build a preview through the standalone SDK's public extraction API."""

    from platen_document import DocumentProcessor, OCRProfile

    processor = DocumentProcessor()
    try:
        result = processor.extract_text(
            input_path,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            profile=OCRProfile.OCR_TEXT_V2,
            routing_policy="FAST",
            cancellation_check=cancellation_check,
            page_progress_callback=lambda done, total, _page: progress_callback(done, total) if progress_callback else None,
        )
    except Exception as exc:
        if exc.__class__.__name__ == "LanguageDetectionUncertainError":
            raise LanguageDetectionUncertainError(str(exc)) from exc
        _translate_sdk_error(exc)
        raise
    failed = next((page for page in result.pages if page.status.value == "FAILED"), None)
    if failed:
        if failed.failure_code == "LanguageDetectionUncertainError":
            raise LanguageDetectionUncertainError("the document language could not be determined reliably")
        if failed.failure_code == "EngineUnavailableError":
            raise OcrMarkupEngineUnavailableError("the OCR engine is unavailable while preparing selectable text")
        if failed.failure_code == "OCRTimeoutError":
            raise OCRTimeoutError("OCR exceeded the markup preview deadline")
        raise WordGeometryUnavailableError("a page did not produce selectable word geometry")
    return project_markup_preview(result, input_path)


def execute_ocr_markup_preview(
    input_path: str | Path,
    *,
    language: str = "eng",
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    cancellation_check: CancellationCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Return temporary selectable words using the configured markup engine."""

    selected = configured_ocr_markup_engine()
    logger.info("OCR_V2_MARKUP_ENGINE consumer=ocr_markup_preview engine=%s", selected)
    executor = _preview_internal if selected == "internal" else _preview_sdk
    return executor(
        input_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        cancellation_check=cancellation_check,
        progress_callback=progress_callback,
    )


__all__ = [
    "DEFAULT_OCR_MARKUP_ENGINE",
    "OCR_MARKUP_ENGINE_ENV",
    "OcrMarkupEngineConfigurationError",
    "OcrMarkupEngineUnavailableError",
    "SUPPORTED_OCR_MARKUP_ENGINES",
    "configured_ocr_markup_engine",
    "execute_ocr_markup",
    "execute_ocr_markup_selection",
    "execute_ocr_markup_preview",
]

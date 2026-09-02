"""Engine boundary for the legacy Highlight/Underline/Strikeout family.

The public legacy routes submit page rectangles.  That contract is different
from the V2 text-query markup consumer, so this selector deliberately sits
above the shared worker markup actor and never reuses ``OCR_MARKUP_ENGINE``.
The SDK branch supplies canonical OCR words to the existing legacy annotation
projection; PDFNest continues to own storage, rectangles, and PDF mutation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import pymupdf as fitz

from app.api.tools.editor.document import is_valid_ocr_word
from app.core.ocr_v2.errors import EngineUnavailableError, OCRTimeoutError
from app.jobs.cancellation import JobCancelledException


LEGACY_MARKUP_OCR_ENGINE_ENV = "LEGACY_MARKUP_OCR_ENGINE"
DEFAULT_LEGACY_MARKUP_OCR_ENGINE = "internal"
SUPPORTED_LEGACY_MARKUP_OCR_ENGINES = frozenset({"internal", "sdk"})
LEGACY_MARKUP_CONSUMER = "legacy_markup"
LEGACY_MARKUP_OCR_ZOOM = 2.0

CancellationCheck = Callable[[], None]
ProgressCallback = Callable[[int, int], None]

logger = logging.getLogger(__name__)


class LegacyMarkupOcrEngineConfigurationError(ValueError):
    """The legacy markup OCR engine selector is unsupported."""


class LegacyMarkupOcrEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected legacy markup SDK cannot be loaded."""


def configured_legacy_markup_ocr_engine(raw: str | None = None) -> str:
    """Return the selector, defaulting to the historical internal path."""

    value = (
        os.getenv(LEGACY_MARKUP_OCR_ENGINE_ENV, DEFAULT_LEGACY_MARKUP_OCR_ENGINE)
        if raw is None
        else raw
    )
    normalized = str(value).strip().lower() or DEFAULT_LEGACY_MARKUP_OCR_ENGINE
    if normalized not in SUPPORTED_LEGACY_MARKUP_OCR_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_LEGACY_MARKUP_OCR_ENGINES))
        raise LegacyMarkupOcrEngineConfigurationError(
            f"{LEGACY_MARKUP_OCR_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _historical_markup(
    input_path: str | Path,
    output_path: str | Path,
    boxes: list[dict[str, Any]],
    action: str,
    mode: str,
    password: str | None,
    progress_callback: ProgressCallback | None,
) -> None:
    """Use the unchanged application-owned legacy markup implementation."""

    from app.api.tools.markup.document import process_markup_pdf

    process_markup_pdf(
        input_path=str(input_path),
        output_path=str(output_path),
        boxes=boxes,
        action=action,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        password=password,
        progress_callback=progress_callback,
    )


def _sdk_processor() -> Any:
    try:
        from platen_document import (
            DocumentProcessor,
            EngineConfiguration,
            RasterDpiMetadataPolicy,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise LegacyMarkupOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise

    # The historical legacy markup OCR helper renders at 2x (144 DPI) and
    # passes a PIL image without a resolution tag to Tesseract.  This is a
    # consumer-local compatibility choice; the SDK default remains unchanged.
    return DocumentProcessor(
        EngineConfiguration(
            max_raster_pixels=25_000_000,
            raster_dpi=int(LEGACY_MARKUP_OCR_ZOOM * 72),
            raster_dpi_metadata_policy=RasterDpiMetadataPolicy.OMIT_DPI,
        )
    )


def _sdk_profile() -> Any:
    from platen_document import OCRProfile

    return OCRProfile.OCR_TEXT_V2


def _translate_sdk_exception(exc: Exception) -> None:
    """Map public SDK failures to safe worker-facing classifications."""

    try:
        from platen_document import (
            EngineUnavailableError as SdkEngineUnavailableError,
            OCRTimeoutError as SdkOCRTimeoutError,
        )
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise LegacyMarkupOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise

    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError("legacy markup OCR engine is unavailable") from exc
    if isinstance(exc, SdkOCRTimeoutError):
        raise OCRTimeoutError("legacy markup OCR exceeded the page deadline") from exc


def _raise_failed_page(result: Any) -> None:
    failed = next(
        (
            page
            for page in result.pages
            if str(getattr(getattr(page, "status", None), "value", getattr(page, "status", "")))
            == "FAILED"
        ),
        None,
    )
    if failed is None:
        return
    if getattr(failed, "failure_code", None) == "EngineUnavailableError":
        raise EngineUnavailableError("legacy markup OCR engine is unavailable")
    if getattr(failed, "failure_code", None) == "OCRTimeoutError":
        raise OCRTimeoutError("legacy markup OCR exceeded the page deadline")
    raise RuntimeError("legacy markup OCR failed for a page")


def _sdk_ocr_word_items(result: Any) -> dict[int, list[dict[str, Any]]]:
    """Project public SDK token geometry into the legacy word-item contract."""

    words_by_page: dict[int, list[dict[str, Any]]] = {}
    for page in result.pages:
        items: list[dict[str, Any]] = []
        geometry = page.geometry
        pixel_width = int(geometry.pixel_width or round(float(geometry.width) * LEGACY_MARKUP_OCR_ZOOM))
        pixel_height = int(geometry.pixel_height or round(float(geometry.height) * LEGACY_MARKUP_OCR_ZOOM))

        def legacy_point(value: float, page_extent: float, pixel_extent: int) -> float:
            pixel_value = value * pixel_extent / page_extent
            return round(pixel_value) / LEGACY_MARKUP_OCR_ZOOM

        for token in page.tokens:
            text = str(getattr(token, "text", "")).strip()
            confidence = getattr(getattr(token, "confidence", None), "raw_value", -1.0)
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = -1.0
            if not text or not is_valid_ocr_word(text, confidence_value):
                continue
            bbox = token.bbox
            rect = fitz.Rect(
                legacy_point(float(bbox.x), float(geometry.width), pixel_width),
                legacy_point(float(bbox.y), float(geometry.height), pixel_height),
                legacy_point(float(bbox.x + bbox.width), float(geometry.width), pixel_width),
                legacy_point(float(bbox.y + bbox.height), float(geometry.height), pixel_height),
            )
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                continue
            items.append({"rect": rect, "text": text, "conf": confidence_value})
        words_by_page[int(page.page_index)] = items
    return words_by_page


def _sdk_execute(
    input_path: str | Path,
    output_path: str | Path,
    boxes: list[dict[str, Any]],
    action: str,
    mode: str,
    password: str | None,
    cancellation_check: CancellationCheck | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    normalized_mode = str(mode or "smart").strip().lower()

    # Manual rectangles and native-only text selection do not invoke OCR in
    # the historical product. Preserve that behavior instead of doing an
    # unnecessary SDK extraction which could change scanned/native routing.
    if normalized_mode not in {"smart", "ocr"}:
        _historical_markup(
            input_path,
            output_path,
            boxes,
            action,
            normalized_mode,
            password,
            progress_callback,
        )
        return {"engine": "non_ocr_historical_projection", "ocr_passes": 0}

    try:
        processor = _sdk_processor()
        result = processor.extract_text(
            input_path,
            password=password,
            language="eng",
            profile=_sdk_profile(),
            routing_policy="FORCE_OCR" if normalized_mode == "ocr" else "FAST",
            cancellation_check=cancellation_check,
        )
    except JobCancelledException:
        raise
    except Exception as exc:
        _translate_sdk_exception(exc)
        raise RuntimeError("legacy markup SDK processing failed") from exc

    _raise_failed_page(result)
    word_items_by_page = _sdk_ocr_word_items(result)

    from app.api.tools.markup.document import process_markup_pdf_with_ocr_words

    process_markup_pdf_with_ocr_words(
        input_path=str(input_path),
        output_path=str(output_path),
        boxes=boxes,
        action=action,  # type: ignore[arg-type]
        mode=normalized_mode,  # type: ignore[arg-type]
        password=password,
        progress_callback=progress_callback,
        ocr_word_items_by_page=word_items_by_page,
    )
    return {
        "engine": "platen_document",
        "ocr_passes": 1,
        "page_count": len(result.pages),
        "word_pages": sum(1 for words in word_items_by_page.values() if words),
    }


def execute_legacy_markup(
    input_path: str | Path,
    output_path: str | Path,
    *,
    boxes: list[dict[str, Any]],
    action: str,
    mode: str = "smart",
    password: str | None = None,
    cancellation_check: CancellationCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute one legacy markup operation behind its own selector."""

    selected = configured_legacy_markup_ocr_engine()
    logger.info("LEGACY_MARKUP_ENGINE consumer=legacy_markup engine=%s", selected)
    if selected == "internal":
        _historical_markup(
            input_path=input_path,
            output_path=output_path,
            boxes=boxes,
            action=action,
            mode=mode,
            password=password,
            progress_callback=progress_callback,
        )
        return {"engine": "internal", "ocr_passes": 1 if str(mode).lower() in {"smart", "ocr"} else 0}
    return _sdk_execute(
        input_path,
        output_path,
        boxes,
        action,
        mode,
        password,
        cancellation_check,
        progress_callback,
    )


__all__ = [
    "DEFAULT_LEGACY_MARKUP_OCR_ENGINE",
    "LEGACY_MARKUP_CONSUMER",
    "LEGACY_MARKUP_OCR_ENGINE_ENV",
    "LegacyMarkupOcrEngineConfigurationError",
    "LegacyMarkupOcrEngineUnavailableError",
    "SUPPORTED_LEGACY_MARKUP_OCR_ENGINES",
    "configured_legacy_markup_ocr_engine",
    "execute_legacy_markup",
]

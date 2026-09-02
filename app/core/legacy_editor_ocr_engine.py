"""Legacy PDF Editor OCR engine selection.

The ordinary PDF Editor shares a worker route with General Editor and Studio.
The explicit legacy_editor caller marker keeps this selector independent from
EDITOR_OCR_ENGINE and from the Studio internal path.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from statistics import median
from typing import Any, Callable

import pymupdf as fitz

from app.core.editor_ocr_projection import first_failed_editor_page, project_editor_result
from app.core.ocr_v2.errors import EngineUnavailableError


LEGACY_EDITOR_OCR_ENGINE_ENV = "LEGACY_EDITOR_OCR_ENGINE"
DEFAULT_LEGACY_EDITOR_OCR_ENGINE = "internal"
SUPPORTED_LEGACY_EDITOR_OCR_ENGINES = frozenset({"internal", "sdk"})
LEGACY_EDITOR_CONSUMER = "legacy_editor"

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]
LEGACY_OCR_ZOOM = 2.0


class LegacyEditorOcrEngineConfigurationError(ValueError):
    """The legacy PDF Editor OCR engine selector is unsupported."""


class LegacyEditorOcrEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected legacy PDF Editor engine cannot be loaded."""


def configured_legacy_editor_ocr_engine(raw: str | None = None) -> str:
    """Return the normalized selector without an implicit runtime fallback."""

    value = (
        os.getenv(LEGACY_EDITOR_OCR_ENGINE_ENV, DEFAULT_LEGACY_EDITOR_OCR_ENGINE)
        if raw is None
        else raw
    )
    normalized = str(value).strip().lower() or DEFAULT_LEGACY_EDITOR_OCR_ENGINE
    if normalized not in SUPPORTED_LEGACY_EDITOR_OCR_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_LEGACY_EDITOR_OCR_ENGINES))
        raise LegacyEditorOcrEngineConfigurationError(
            f"{LEGACY_EDITOR_OCR_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_execute(
    input_path: str | Path,
    password: str | None,
    *,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    """Call the unchanged legacy PDF Editor extraction implementation."""

    del page_progress_callback
    from app.api.tools.editor.document import extract_document

    return extract_document(
        str(input_path),
        password,
        cancellation_check=cancellation_check,
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
            raise LegacyEditorOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise

    # The historical legacy Editor OCR rasterizes at 2x (144 DPI). Keep that
    # input raster contract while the SDK supplies canonical recognition.
    return DocumentProcessor(
        EngineConfiguration(
            max_raster_pixels=25_000_000,
            raster_dpi=int(LEGACY_OCR_ZOOM * 72),
            raster_dpi_metadata_policy=RasterDpiMetadataPolicy.OMIT_DPI,
        )
    )


def _sdk_profile() -> Any:
    from platen_document import OCRProfile

    return OCRProfile.OCR_TEXT_V2


def _translate_sdk_exception(exc: Exception) -> None:
    try:
        from platen_document import EngineUnavailableError as SdkEngineUnavailableError
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise LegacyEditorOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise

    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError(
            "legacy PDF Editor OCR engine is unavailable"
        ) from exc


def _raise_failed_page(result: Any) -> None:
    failed = first_failed_editor_page(result)
    if failed is None:
        return
    if getattr(failed, "failure_code", None) == "EngineUnavailableError":
        raise EngineUnavailableError("legacy PDF Editor OCR engine is unavailable")
    raise RuntimeError("legacy PDF Editor OCR extraction failed for a page")


def _valid_legacy_ocr_word(text: str, confidence: float) -> bool:
    if confidence < 30.0:
        return False
    if len(text) == 1 and not text.isalnum():
        return False
    return not re.match(r"^[\-_|\\/\.\,\;\:\'\"]+$", text)


def _legacy_ocr_items(page: Any) -> list[dict[str, Any]]:
    """Convert SDK OCR tokens to the legacy Editor's 2x coordinate contract."""

    geometry = page.geometry
    pixel_width = int(geometry.pixel_width or round(float(geometry.width) * LEGACY_OCR_ZOOM))
    pixel_height = int(geometry.pixel_height or round(float(geometry.height) * LEGACY_OCR_ZOOM))

    def legacy_point(value: float, page_extent: float, pixel_extent: int) -> float:
        pixel_value = value * pixel_extent / page_extent
        return round(pixel_value) / LEGACY_OCR_ZOOM

    items: list[dict[str, Any]] = []

    for token in page.tokens:
        confidence = getattr(getattr(token, "confidence", None), "raw_value", -1.0)
        text = str(getattr(token, "text", "")).strip()
        if not _valid_legacy_ocr_word(text, float(confidence)):
            continue
        bbox = token.bbox
        x0 = legacy_point(float(bbox.x), float(geometry.width), pixel_width)
        y0 = legacy_point(float(bbox.y), float(geometry.height), pixel_height)
        x1 = legacy_point(float(bbox.x + bbox.width), float(geometry.width), pixel_width)
        y1 = legacy_point(float(bbox.y + bbox.height), float(geometry.height), pixel_height)
        rect = fitz.Rect(x0, y0, x1, y1)
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            continue
        items.append({"text": text, "rect": rect})
    return items


def _legacy_ocr_lines(word_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not word_items:
        return []

    heights = [max(1.0, float(item["rect"].height)) for item in word_items]
    median_height = median(heights)
    y_tolerance = max(3.0, median_height * 0.45)
    max_x_gap = 10.0
    lines: list[dict[str, Any]] = []

    sorted_words = sorted(
        word_items,
        key=lambda item: (
            round((item["rect"].y0 + item["rect"].y1) / 2.0, 3),
            item["rect"].x0,
        ),
    )
    for item in sorted_words:
        rect = item["rect"]
        item_y_center = (rect.y0 + rect.y1) / 2.0
        placed = False
        for line in lines:
            line_y_center = (line["y0"] + line["y1"]) / 2.0
            if abs(item_y_center - line_y_center) <= y_tolerance:
                gap = rect.x0 - line["x1"]
                if 0 <= gap <= max_x_gap:
                    line["items"].append(item)
                    line["x0"] = min(line["x0"], rect.x0)
                    line["x1"] = max(line["x1"], rect.x1)
                    line["y0"] = min(line["y0"], rect.y0)
                    line["y1"] = max(line["y1"], rect.y1)
                    placed = True
                    break
        if not placed:
            lines.append(
                {
                    "items": [item],
                    "x0": rect.x0,
                    "x1": rect.x1,
                    "y0": rect.y0,
                    "y1": rect.y1,
                }
            )
    return lines


def _legacy_ocr_page_projection(page: Any) -> dict[str, Any]:
    items = _legacy_ocr_items(page)
    elements: list[dict[str, Any]] = []
    for line in _legacy_ocr_lines(items):
        ordered_items = sorted(line["items"], key=lambda item: item["rect"].x0)
        text = " ".join(str(item["text"]) for item in ordered_items).strip()
        if not text:
            continue
        rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
        if rect.is_empty or rect.width < 3.0:
            continue
        max_height = max(
            (float(item["rect"].height) for item in ordered_items),
            default=rect.height,
        )
        elements.append(
            {
                "text": text,
                "original_text": text,
                "x": rect.x0,
                "y": rect.y0,
                "width": rect.width,
                "height": rect.height,
                "size": round(max_height * 1.15, 1),
                "font": "tiro",
                "bg_color": "transparent",
                "text_color": "#000000",
                "transparent_bg": True,
            }
        )
    from app.api.tools.editor.document import deduplicate_elements

    elements = deduplicate_elements(elements)
    for index, element in enumerate(elements, start=1):
        element["id"] = f"p{page.page_index + 1}-text-{index}"

    return {
        "elements": elements,
        "kind": "scanned" if elements else "blank",
        "is_ocr": True,
        "has_selectable_text": False,
        "word_count": 0,
        "text_block_count": 0,
        "image_block_count": 0,
    }


def _legacy_native_page_projections(
    input_path: str | Path,
    password: str | None,
    cancellation_check: CancellationCheck | None,
) -> list[dict[str, Any] | None]:
    """Preserve the legacy native-first contract without running OCR."""

    from app.api.tools.editor.document import extract_native_page

    with fitz.open(str(input_path)) as document:
        if document.needs_pass:
            if not password or document.authenticate(password) <= 0:
                raise RuntimeError("Invalid PDF password")
        pages: list[dict[str, Any] | None] = []
        for page_index, page in enumerate(document):
            if cancellation_check is not None:
                cancellation_check()
            if page.get_text("words"):
                pages.append(extract_native_page(page, page_index + 1))
            else:
                pages.append(None)
        return pages


def _project_legacy_editor_result(
    result: Any,
    native_pages: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Keep SDK OCR while matching the historical legacy page projection."""

    projected = project_editor_result(result)
    for page_index, (projected_page, canonical_page) in enumerate(zip(projected["pages"], result.pages)):
        if native_pages and page_index < len(native_pages) and native_pages[page_index] is not None:
            projected["pages"][page_index] = native_pages[page_index]
            continue
        processing_source = getattr(canonical_page, "processing_source", None)
        source = getattr(processing_source, "value", processing_source)
        if source == "OCR_RECOGNITION":
            projected_page.update(_legacy_ocr_page_projection(canonical_page))
    return projected


def _sdk_execute(
    input_path: str | Path,
    password: str | None,
    *,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> dict[str, Any]:
    native_pages = None
    if Path(input_path).is_file():
        try:
            native_pages = _legacy_native_page_projections(
                input_path,
                password,
                cancellation_check,
            )
        except fitz.FileDataError:
            native_pages = None
    if native_pages and all(page is not None for page in native_pages):
        return {"success": True, "pages": [page for page in native_pages if page is not None]}

    processor = _sdk_processor()
    try:
        result = processor.extract_text(
            input_path,
            password=password,
            language="eng",
            profile=_sdk_profile(),
            routing_policy="FAST",
            cancellation_check=cancellation_check,
            page_progress_callback=page_progress_callback,
        )
    except Exception as exc:
        _translate_sdk_exception(exc)
        raise

    _raise_failed_page(result)
    return _project_legacy_editor_result(result, native_pages)


def execute_legacy_editor_ocr(
    input_path: str | Path,
    password: str | None = None,
    *,
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> Any:
    """Execute ordinary PDF Editor OCR through the selected implementation."""

    selected = configured_legacy_editor_ocr_engine()
    logger.info(
        "OCR_V2_LEGACY_EDITOR_ENGINE consumer=legacy_editor engine=%s",
        selected,
    )
    if selected == "internal":
        return _internal_execute(
            input_path,
            password,
            cancellation_check=cancellation_check,
            page_progress_callback=page_progress_callback,
        )
    return _sdk_execute(
        input_path,
        password,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )


__all__ = [
    "DEFAULT_LEGACY_EDITOR_OCR_ENGINE",
    "LEGACY_EDITOR_CONSUMER",
    "LEGACY_EDITOR_OCR_ENGINE_ENV",
    "LegacyEditorOcrEngineConfigurationError",
    "LegacyEditorOcrEngineUnavailableError",
    "SUPPORTED_LEGACY_EDITOR_OCR_ENGINES",
    "configured_legacy_editor_ocr_engine",
    "execute_legacy_editor_ocr",
]

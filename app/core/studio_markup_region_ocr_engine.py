"""Controlled Studio OCR-aware region-markup engine selection.

This boundary belongs only to the Studio ``ocr_v2`` region-markup payload. It
keeps the historical Studio processor as the default and uses the standalone
SDK only through its public region-markup API when explicitly selected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.api.tools.markup.utils import normalize_hex
from app.core.ocr_v2.errors import (
    AnnotationWriteError,
    EngineUnavailableError,
    MarkupError,
    OCRTimeoutError,
    TextNotFoundError,
    WordGeometryUnavailableError,
)


STUDIO_MARKUP_REGION_OCR_ENGINE_ENV = "STUDIO_MARKUP_REGION_OCR_ENGINE"
DEFAULT_STUDIO_MARKUP_REGION_OCR_ENGINE = "internal"
SUPPORTED_STUDIO_MARKUP_REGION_OCR_ENGINES = frozenset({"internal", "sdk"})
STUDIO_MARKUP_REGION_OCR_ROUTE = "studio_ocr_v2"

CancellationCheck = Callable[[], None]
ProgressCallback = Callable[[int, int], None]

logger = logging.getLogger(__name__)


class StudioMarkupRegionOcrEngineConfigurationError(ValueError):
    """The Studio region-markup engine selector is unsupported."""


class StudioMarkupRegionOcrEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected Studio region-markup SDK cannot be loaded."""


def configured_studio_markup_region_ocr_engine(raw: str | None = None) -> str:
    value = (
        os.getenv(
            STUDIO_MARKUP_REGION_OCR_ENGINE_ENV,
            DEFAULT_STUDIO_MARKUP_REGION_OCR_ENGINE,
        )
        if raw is None
        else raw
    )
    normalized = str(value).strip().lower() or DEFAULT_STUDIO_MARKUP_REGION_OCR_ENGINE
    if normalized not in SUPPORTED_STUDIO_MARKUP_REGION_OCR_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_STUDIO_MARKUP_REGION_OCR_ENGINES))
        raise StudioMarkupRegionOcrEngineConfigurationError(
            f"{STUDIO_MARKUP_REGION_OCR_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _normalized_action(action: str | object) -> str:
    value = str(getattr(action, "value", action)).strip().lower()
    if value not in {"highlight", "underline", "strikeout"}:
        raise ValueError(f"unsupported Studio markup action: {value}")
    return value


def _normalized_mode(mode: str | object) -> str:
    value = str(getattr(mode, "value", mode)).strip().lower() or "smart"
    if value not in {"manual", "smart", "ocr"}:
        raise ValueError(f"unsupported Studio markup mode: {value}")
    return value


def _internal_execute(
    input_path: str | Path,
    output_path: str | Path,
    boxes: list[dict[str, Any]],
    action: str,
    mode: str,
    password: str | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Call the unchanged frozen/internal Studio region implementation."""
    from app.api.tools.markup.document import process_markup_pdf_v2_regions

    return process_markup_pdf_v2_regions(
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
        from platen_document import DocumentProcessor, EngineConfiguration
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise StudioMarkupRegionOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor(EngineConfiguration(max_raster_pixels=25_000_000))


def _sdk_public_types() -> tuple[Any, Any, Any, Any]:
    try:
        from platen_document import MarkupAction, MarkupMode, MarkupRegion, Rect
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise StudioMarkupRegionOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return MarkupAction, MarkupMode, MarkupRegion, Rect


def _sdk_regions(boxes: Sequence[Mapping[str, Any]], markup_region: Any, rect: Any) -> tuple[Any, ...]:
    regions: list[Any] = []
    for index, box in enumerate(boxes):
        try:
            page_number = int(box.get("page", 0))
            x = float(box.get("x", 0))
            y = float(box.get("y", 0))
            width = float(box.get("width", 0))
            height = float(box.get("height", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Studio markup box {index} has invalid geometry") from exc
        color = normalize_hex(str(box.get("color", "#FFFF00")), "#FFFF00")
        regions.append(
            markup_region(
                page_number=page_number,
                rect=rect(x=x, y=y, width=width, height=height),
                region_id=str(box["id"]) if box.get("id") is not None else None,
                color=color,
            )
        )
    return tuple(regions)


def _translate_sdk_exception(exc: Exception) -> None:
    try:
        from platen_document import (
            AnnotationWriteError as SdkAnnotationWriteError,
            EngineUnavailableError as SdkEngineUnavailableError,
            MarkupError as SdkMarkupError,
            OCRTimeoutError as SdkOCRTimeoutError,
            TextNotFoundError as SdkTextNotFoundError,
            WordGeometryUnavailableError as SdkWordGeometryUnavailableError,
        )
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise StudioMarkupRegionOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise
    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError("Studio region-markup engine is unavailable") from exc
    if isinstance(exc, SdkOCRTimeoutError):
        raise OCRTimeoutError("Studio region-markup OCR exceeded the page deadline") from exc
    if isinstance(exc, SdkTextNotFoundError):
        raise TextNotFoundError(str(exc)) from exc
    if isinstance(exc, SdkWordGeometryUnavailableError):
        raise WordGeometryUnavailableError(str(exc)) from exc
    if isinstance(exc, SdkAnnotationWriteError):
        raise AnnotationWriteError(str(exc)) from exc
    if isinstance(exc, SdkMarkupError):
        raise MarkupError(str(exc)) from exc


def _project_sdk_result(execution: Any) -> dict[str, Any]:
    selections: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for region in execution.regions:
        selection = region.selection.to_dict() if region.selection else None
        if selection is not None:
            selection["region_id"] = region.region_id
            selection["color"] = list(region.color)
            selections.append(selection)
        regions.append(
            {
                "region_index": region.region_index,
                "region_id": region.region_id,
                "page": region.page_number,
                "status": region.status.value,
                "color": list(region.color),
                "annotation_count": region.annotation_count,
                "word_ids": list(region.word_ids),
                "selected_text": region.selected_text,
                "annotation_rects": [
                    {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
                    for rect in region.annotation_rects
                ],
            }
        )
    return {
        "source_policy": execution.source_policy,
        "selection_count": len(selections),
        "selections": selections,
        "regions": regions,
        "annotation_count": execution.annotation_count,
        "page_count": execution.page_count,
        "document_result_reused": execution.document_result_reused,
        "extraction_performed": execution.extraction_performed,
    }


def _preserve_empty_selection_contract(execution: Any, projected: Mapping[str, Any], mode: str) -> None:
    """Keep the internal Studio failure for native text excluded by OCR mode.

    The SDK deliberately returns typed ``NO_WORDS`` region outcomes. The
    historical Studio path raises ``TextNotFoundError`` instead when a text
    bearing native page has no words eligible for its smart/OCR selection.
    Preserve that worker-facing contract without changing the public SDK.
    """
    if projected["selection_count"]:
        return
    page_sources = getattr(execution, "page_sources", ())
    if mode in {"smart", "ocr"} and any(source.get("source_type") == "native" for source in page_sources):
        raise TextNotFoundError("no canonical words intersected the selected regions")


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
    markup_action, markup_mode, markup_region, rect = _sdk_public_types()
    regions = _sdk_regions(boxes, markup_region, rect)
    processor = _sdk_processor()
    selected_mode = markup_mode(mode)

    try:
        execution = processor.apply_markup_regions(
            input_path,
            output_path,
            action=markup_action(action),
            regions=regions,
            mode=selected_mode,
            password=password,
            language="eng",
            routing_policy="FAST",
            cancellation_check=cancellation_check,
            page_progress_callback=(
                (lambda done, total, _page: progress_callback(done, total))
                if progress_callback and mode != "manual"
                else None
            ),
            progress_callback=progress_callback if mode == "manual" else None,
        )
    except Exception as exc:
        _translate_sdk_exception(exc)
        raise
    projected = _project_sdk_result(execution)
    _preserve_empty_selection_contract(execution, projected, mode)
    return projected


def execute_studio_markup_region_ocr(
    input_path: str | Path,
    output_path: str | Path,
    *,
    boxes: list[dict[str, Any]],
    action: str | object,
    mode: str | object = "smart",
    password: str | None = None,
    cancellation_check: CancellationCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute only the Studio ``ocr_v2`` rectangle-markup boundary."""
    selected = configured_studio_markup_region_ocr_engine()
    normalized_action = _normalized_action(action)
    normalized_mode = _normalized_mode(mode)
    logger.info("OCR_V2_STUDIO_MARKUP_REGION_ENGINE route=studio_ocr_v2 engine=%s", selected)
    if selected == "internal":
        return _internal_execute(
            input_path,
            output_path,
            boxes,
            normalized_action,
            normalized_mode,
            password,
            progress_callback,
        )
    return _sdk_execute(
        input_path,
        output_path,
        boxes,
        normalized_action,
        normalized_mode,
        password,
        cancellation_check,
        progress_callback,
    )


__all__ = [
    "DEFAULT_STUDIO_MARKUP_REGION_OCR_ENGINE",
    "STUDIO_MARKUP_REGION_OCR_ENGINE_ENV",
    "STUDIO_MARKUP_REGION_OCR_ROUTE",
    "SUPPORTED_STUDIO_MARKUP_REGION_OCR_ENGINES",
    "StudioMarkupRegionOcrEngineConfigurationError",
    "StudioMarkupRegionOcrEngineUnavailableError",
    "configured_studio_markup_region_ocr_engine",
    "execute_studio_markup_region_ocr",
]

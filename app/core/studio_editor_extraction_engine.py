"""Controlled Studio editor extraction engine selection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from app.core.editor_ocr_projection import first_failed_editor_page, project_editor_result
from app.core.ocr_v2.errors import EngineUnavailableError


STUDIO_EDITOR_EXTRACTION_ENGINE_ENV = "STUDIO_EDITOR_EXTRACTION_ENGINE"
DEFAULT_STUDIO_EDITOR_EXTRACTION_ENGINE = "internal"
SUPPORTED_STUDIO_EDITOR_EXTRACTION_ENGINES = frozenset({"internal", "sdk"})
STUDIO_EDITOR_EXTRACTION_CONSUMER = "studio"

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]


class StudioEditorExtractionEngineConfigurationError(ValueError):
    """The Studio editor extraction selector is unsupported."""


class StudioEditorExtractionEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected Studio editor engine cannot be loaded."""


def configured_studio_editor_extraction_engine(raw: str | None = None) -> str:
    value = (
        os.getenv(
            STUDIO_EDITOR_EXTRACTION_ENGINE_ENV,
            DEFAULT_STUDIO_EDITOR_EXTRACTION_ENGINE,
        )
        if raw is None
        else raw
    )
    normalized = str(value).strip().lower() or DEFAULT_STUDIO_EDITOR_EXTRACTION_ENGINE
    if normalized not in SUPPORTED_STUDIO_EDITOR_EXTRACTION_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_STUDIO_EDITOR_EXTRACTION_ENGINES))
        raise StudioEditorExtractionEngineConfigurationError(
            f"{STUDIO_EDITOR_EXTRACTION_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_execute(
    input_path: str | Path,
    password: str | None,
    *,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    from app.api.tools.editor.document import extract_document_v2

    return extract_document_v2(
        str(input_path),
        password,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )


def _sdk_processor() -> Any:
    try:
        from platen_document import DocumentProcessor, EngineConfiguration
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise StudioEditorExtractionEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor(EngineConfiguration(max_raster_pixels=25_000_000))


def _sdk_profile() -> Any:
    from platen_document import OCRProfile

    return OCRProfile.OCR_TEXT_V2


def _translate_sdk_exception(exc: Exception) -> None:
    try:
        from platen_document import EngineUnavailableError as SdkEngineUnavailableError
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise StudioEditorExtractionEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise
    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError(
            "Studio editor extraction engine is unavailable"
        ) from exc


def _raise_failed_page(result: Any) -> None:
    failed = first_failed_editor_page(result)
    if failed is None:
        return
    if getattr(failed, "failure_code", None) == "EngineUnavailableError":
        raise EngineUnavailableError("Studio editor extraction engine is unavailable")
    raise RuntimeError("Studio editor extraction failed for a page")


def _sdk_execute(
    input_path: str | Path,
    password: str | None,
    *,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> dict[str, Any]:
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
    return project_editor_result(result)


def execute_studio_editor_extraction(
    input_path: str | Path,
    password: str | None = None,
    *,
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> Any:
    selected = configured_studio_editor_extraction_engine()
    logger.info("OCR_V2_STUDIO_EDITOR_EXTRACTION_ENGINE consumer=studio engine=%s", selected)
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
    "DEFAULT_STUDIO_EDITOR_EXTRACTION_ENGINE",
    "STUDIO_EDITOR_EXTRACTION_CONSUMER",
    "STUDIO_EDITOR_EXTRACTION_ENGINE_ENV",
    "SUPPORTED_STUDIO_EDITOR_EXTRACTION_ENGINES",
    "StudioEditorExtractionEngineConfigurationError",
    "StudioEditorExtractionEngineUnavailableError",
    "configured_studio_editor_extraction_engine",
    "execute_studio_editor_extraction",
]

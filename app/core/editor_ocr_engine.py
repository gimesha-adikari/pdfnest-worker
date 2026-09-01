"""General Editor OCR V2 engine selection.

This is intentionally a consumer-specific boundary.  Studio uses the same
worker extraction endpoint, but its request is marked as ``studio`` and stays
on the frozen internal implementation while General Editor can opt into the
standalone SDK.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from app.core.editor_ocr_projection import first_failed_editor_page, project_editor_result
from app.core.ocr_v2.errors import EngineUnavailableError


EDITOR_OCR_ENGINE_ENV = "EDITOR_OCR_ENGINE"
DEFAULT_EDITOR_OCR_ENGINE = "internal"
SUPPORTED_EDITOR_OCR_ENGINES = frozenset({"internal", "sdk"})

EDITOR_OCR_CONSUMER_GENERAL_EDITOR = "general_editor"
EDITOR_OCR_CONSUMER_STUDIO = "studio"
EDITOR_OCR_CONSUMER_LEGACY = "legacy"
SUPPORTED_EDITOR_OCR_CONSUMERS = frozenset(
    {
        EDITOR_OCR_CONSUMER_GENERAL_EDITOR,
        EDITOR_OCR_CONSUMER_STUDIO,
        EDITOR_OCR_CONSUMER_LEGACY,
    }
)

logger = logging.getLogger(__name__)

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]


class EditorOcrEngineConfigurationError(ValueError):
    """The General Editor OCR engine selector is unsupported."""


class EditorOcrEngineUnavailableError(EngineUnavailableError):
    """The explicitly selected General Editor OCR engine cannot be loaded."""


def configured_editor_ocr_engine(raw: str | None = None) -> str:
    """Return the normalized selector without an implicit runtime fallback."""

    value = os.getenv(EDITOR_OCR_ENGINE_ENV, DEFAULT_EDITOR_OCR_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_EDITOR_OCR_ENGINE
    if normalized not in SUPPORTED_EDITOR_OCR_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_EDITOR_OCR_ENGINES))
        raise EditorOcrEngineConfigurationError(
            f"{EDITOR_OCR_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _internal_execute(
    input_path: str | Path,
    password: str | None,
    *,
    cancellation_check: CancellationCheck | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    """Call the unchanged internal Editor V2 implementation."""

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
            raise EditorOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise

    # Match the current internal Editor V2 raster guard and keep the SDK route
    # Tesseract-only below, as the current editor contract requires.
    return DocumentProcessor(EngineConfiguration(max_raster_pixels=25_000_000))


def _sdk_profile() -> Any:
    from platen_document import OCRProfile

    return OCRProfile.OCR_TEXT_V2


def _translate_sdk_exception(exc: Exception) -> None:
    try:
        from platen_document import EngineUnavailableError as SdkEngineUnavailableError
    except ModuleNotFoundError as import_error:
        if import_error.name == "platen_document":
            raise EditorOcrEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from import_error
        raise

    if isinstance(exc, SdkEngineUnavailableError):
        raise EngineUnavailableError(
            "OCR V2 editor extraction engine is unavailable"
        ) from exc


def _raise_failed_page(result: Any) -> None:
    failed = first_failed_editor_page(result)
    if failed is None:
        return
    if getattr(failed, "failure_code", None) == "EngineUnavailableError":
        raise EngineUnavailableError("OCR V2 editor extraction engine is unavailable")
    raise RuntimeError("OCR V2 editor extraction failed for a page")


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
            # The existing Editor V2 path explicitly uses the Tesseract route.
            routing_policy="FAST",
            cancellation_check=cancellation_check,
            page_progress_callback=page_progress_callback,
        )
    except Exception as exc:
        _translate_sdk_exception(exc)
        raise

    _raise_failed_page(result)
    return project_editor_result(result)


def execute_editor_ocr(
    input_path: str | Path,
    password: str | None = None,
    *,
    cancellation_check: CancellationCheck | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> Any:
    """Execute General Editor OCR V2 through the selected implementation."""

    selected = configured_editor_ocr_engine()
    logger.info("OCR_V2_EDITOR_ENGINE consumer=general_editor engine=%s", selected)
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
    "DEFAULT_EDITOR_OCR_ENGINE",
    "EDITOR_OCR_CONSUMER_GENERAL_EDITOR",
    "EDITOR_OCR_CONSUMER_LEGACY",
    "EDITOR_OCR_CONSUMER_STUDIO",
    "EDITOR_OCR_ENGINE_ENV",
    "EditorOcrEngineConfigurationError",
    "EditorOcrEngineUnavailableError",
    "SUPPORTED_EDITOR_OCR_CONSUMERS",
    "SUPPORTED_EDITOR_OCR_ENGINES",
    "configured_editor_ocr_engine",
    "execute_editor_ocr",
]

"""Controlled OCR Text V2 engine selection for PDFNest consumers.

The internal OCR V2 worker remains the default and the rollback reference.
The SDK branch is deliberately opt-in and is reached only through the
standalone package's public ``DocumentProcessor`` API.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


OCR_TEXT_ENGINE_ENV = "OCR_TEXT_ENGINE"
DEFAULT_OCR_TEXT_ENGINE = "internal"
SUPPORTED_OCR_TEXT_ENGINES = frozenset({"internal", "sdk"})
SUPPORTED_ROUTING_POLICIES = frozenset({"AUTO", "FAST", "QUALITY", "GEOMETRY", "LANGUAGE_FALLBACK"})

CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, object], None]


class OCRTextEngineConfigurationError(ValueError):
    """The OCR Text V2 engine selector is missing or unsupported."""


class OCRTextEngineUnavailableError(RuntimeError):
    """The explicitly selected OCR Text V2 engine cannot be loaded."""


def configured_ocr_text_engine(raw: str | None = None) -> str:
    """Return the normalized configured engine, rejecting invalid values.

    An absent or blank setting is the safe internal default.  Any non-blank
    value must name one of the two controlled implementations; there is no
    implicit fallback from an explicitly selected SDK engine.
    """
    value = os.getenv(OCR_TEXT_ENGINE_ENV, DEFAULT_OCR_TEXT_ENGINE) if raw is None else raw
    normalized = str(value).strip().lower() or DEFAULT_OCR_TEXT_ENGINE
    if normalized not in SUPPORTED_OCR_TEXT_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_OCR_TEXT_ENGINES))
        raise OCRTextEngineConfigurationError(
            f"{OCR_TEXT_ENGINE_ENV} must be one of: {supported}"
        )
    return normalized


def _normalized_routing_policy(value: str | object | None) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "AUTO").strip().upper()


def _route_policy(value: str | object | None) -> Any:
    """Build the same route policy used by the existing OCR Text route."""
    normalized = _normalized_routing_policy(value)
    _validate_routing_policy(normalized)
    from app.core.ocr_v2.routing import RoutePolicy

    if normalized in {"FAST", "LANGUAGE_FALLBACK"}:
        return RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2")
    if normalized in {"AUTO", "QUALITY", "GEOMETRY"}:
        return RoutePolicy(preferred_engine="ppocrv6_medium_v2", fallback_engine="tesseract_v2")
    raise AssertionError(f"unhandled validated OCR routing policy: {normalized}")


def _validate_routing_policy(value: str) -> None:
    if value not in SUPPORTED_ROUTING_POLICIES:
        raise ValueError(f"unsupported OCR routing policy: {value}")


def _execute_internal(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    routing_policy: str | object | None,
    cancellation_check: CancellationCheck | None,
    page_timeout_seconds: float | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    from app.core.ocr_v2.validation import OCRProfile

    worker = _internal_worker(_route_policy(routing_policy))
    return worker.process_document(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        profile=OCRProfile.OCR_TEXT_V2,
        cancellation_check=cancellation_check,
        page_timeout_seconds=page_timeout_seconds,
        page_progress_callback=page_progress_callback,
    )


def _internal_worker(route_policy: Any) -> Any:
    """Construct the frozen internal worker behind the consumer boundary."""
    from app.core.ocr_v2 import OCRV2Worker

    return OCRV2Worker(route_policy=route_policy)


def _sdk_processor() -> Any:
    try:
        from platen_document import DocumentProcessor
    except ModuleNotFoundError as exc:
        if exc.name == "platen_document":
            raise OCRTextEngineUnavailableError(
                "the standalone platen_document package is not installed"
            ) from exc
        raise
    return DocumentProcessor()


def _execute_sdk(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None,
    languages: Sequence[str] | None,
    language_usage: Mapping[str, float] | None,
    routing_policy: str | object | None,
    cancellation_check: CancellationCheck | None,
    page_timeout_seconds: float | None,
    page_progress_callback: PageProgressCallback | None,
) -> Any:
    normalized_policy = _normalized_routing_policy(routing_policy)
    _validate_routing_policy(normalized_policy)
    processor = _sdk_processor()
    return processor.extract_text(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=normalized_policy,
        cancellation_check=cancellation_check,
        page_timeout_seconds=page_timeout_seconds,
        page_progress_callback=page_progress_callback,
    )


def execute_ocr_text(
    pdf_path: str | Path,
    *,
    language: str,
    language_mode: str | None = None,
    languages: Sequence[str] | None = None,
    language_usage: Mapping[str, float] | None = None,
    routing_policy: str | object | None = "AUTO",
    cancellation_check: CancellationCheck | None = None,
    page_timeout_seconds: float | None = None,
    page_progress_callback: PageProgressCallback | None = None,
) -> Any:
    """Execute OCR Text V2 through the explicitly selected implementation."""
    selected = configured_ocr_text_engine()
    if selected == "internal":
        return _execute_internal(
            pdf_path,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            routing_policy=routing_policy,
            cancellation_check=cancellation_check,
            page_timeout_seconds=page_timeout_seconds,
            page_progress_callback=page_progress_callback,
        )
    return _execute_sdk(
        pdf_path,
        language=language,
        language_mode=language_mode,
        languages=languages,
        language_usage=language_usage,
        routing_policy=routing_policy,
        cancellation_check=cancellation_check,
        page_timeout_seconds=page_timeout_seconds,
        page_progress_callback=page_progress_callback,
    )


__all__ = [
    "DEFAULT_OCR_TEXT_ENGINE",
    "OCR_TEXT_ENGINE_ENV",
    "OCRTextEngineConfigurationError",
    "OCRTextEngineUnavailableError",
    "SUPPORTED_OCR_TEXT_ENGINES",
    "configured_ocr_text_engine",
    "execute_ocr_text",
]

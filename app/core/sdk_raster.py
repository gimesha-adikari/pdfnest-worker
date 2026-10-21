"""Fail-closed SDK raster capability boundary for worker consumers."""
from __future__ import annotations

from functools import wraps
from typing import Any

from app.core.ocr_v2.errors import ConfigurationError, EngineUnavailableError, RasterAdmissionError
from app.core.ocr_v2.geometry import _raster_context


class _GuardedProcessor:
    def __init__(self, processor: Any, sdk: Any, policy: Any) -> None:
        self._processor = processor
        self._sdk = sdk
        self._policy = policy

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._processor, name)
        if not callable(value):
            return value

        @wraps(value)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            limit, cancel = _raster_context.get()
            cancel = kwargs.get("cancellation_check") or cancel
            cancellation_error: Exception | None = None

            def check() -> None:
                nonlocal cancellation_error
                try:
                    if cancel is not None:
                        cancel()
                except Exception as exc:
                    # SDK renderers recognize their own cancellation type.
                    # Restore the application's original exception at exit.
                    cancellation_error = exc
                    raise self._sdk.OCRCancellationError("OCR processing cancelled") from exc

            if kwargs.get("cancellation_check") is not None:
                kwargs["cancellation_check"] = check
            try:
                # SDK and worker ContextVars are independent. Explicitly carry
                # the worker's smaller active budget into fresh SDK workers.
                with self._policy(limit, check if cancel is not None else None):
                    return value(*args, **kwargs)
            except self._sdk.OCRCancellationError:
                if cancellation_error is not None:
                    raise cancellation_error
                raise
            except self._sdk.RasterAdmissionError as exc:
                raise RasterAdmissionError("Document raster exceeds safety limits") from exc
            except self._sdk.ConfigurationError as exc:
                raise ConfigurationError("OCR raster configuration is invalid") from exc
        return invoke


def guard_sdk_processor(
    processor_type: Any, config: Any = None, *, unavailable_error: type[Exception] = EngineUnavailableError,
) -> Any:
    """Require the reviewed raster contract before constructing any SDK engine.

    The installed SDK must supply the complete reviewed security contract.
    Incapable packages (including 0.1.8) remain unavailable; no version/policy
    fallback or automatic installation is performed here.
    """
    try:
        import platen_document as sdk
        from platen_document.engine import geometry
        if (not isinstance(getattr(sdk, "RasterAdmissionError", None), type)
                or not isinstance(getattr(sdk, "ConfigurationError", None), type)
                or not isinstance(getattr(sdk, "OCRCancellationError", None), type)
                or getattr(geometry, "HARD_MAX_RASTER_PIXELS", None) != 10_000_000
                or getattr(geometry, "MAX_RASTER_DPI", None) != 600
                or getattr(geometry, "MAX_RASTER_AXIS", None) != 32_768
                or not callable(getattr(geometry, "raster_policy", None))):
            raise ImportError("SDK raster admission contract unavailable")
    except (ImportError, AttributeError) as exc:
        raise unavailable_error("Selected OCR engine is unavailable") from exc
    try:
        processor = processor_type() if config is None else processor_type(config)
    except sdk.ConfigurationError as exc:
        raise ConfigurationError("OCR raster configuration is invalid") from exc
    return _GuardedProcessor(processor, sdk, geometry.raster_policy)

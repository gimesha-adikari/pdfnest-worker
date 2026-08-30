"""Typed failures for the OCR V2 worker-core boundary."""

from __future__ import annotations


class OCRV2Error(Exception):
    """Base class for failures safe to classify at the page/document boundary."""


class ConfigurationError(OCRV2Error, ValueError):
    pass


class EngineUnavailableError(OCRV2Error):
    pass


class NativeTextUndecidedError(OCRV2Error):
    pass


class PageValidationError(OCRV2Error):
    pass


class DocumentValidationError(OCRV2Error):
    pass


class RenderingNotEligibleError(OCRV2Error):
    """Fail-closed render/validation error with safe forensic context."""

    def __init__(
        self,
        message: str,
        *,
        substage: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.substage = substage
        self.reason_code = reason_code


class OCRCancellationError(OCRV2Error):
    pass


class OCRTimeoutError(OCRV2Error):
    pass

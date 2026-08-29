"""Product-profile capability requirements."""

from __future__ import annotations

from .contracts import DocumentResult, ResultCapability
from .validation import OCRProfile, profile_disposition


def product_verdict(result: DocumentResult, profile: OCRProfile) -> str:
    """Return the frozen taxonomy used by Phase 3A reports."""

    return profile_disposition(result, profile)


def searchable_pdf_reason(result: DocumentResult) -> str | None:
    if ResultCapability.WORD_GEOMETRY.value not in result.capabilities:
        return "WORD_GEOMETRY_NOT_AVAILABLE"
    return None

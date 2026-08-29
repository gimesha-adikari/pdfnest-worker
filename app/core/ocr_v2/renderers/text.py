"""Deterministic text renderer over validated canonical results."""

from __future__ import annotations

from ..contracts import DocumentResult
from ..validation import OCRProfile, require_profile


class TextRenderer:
    def render(self, result: DocumentResult) -> str:
        checked = require_profile(result, OCRProfile.OCR_TEXT_V2)
        return "\n\n".join(page.text.rstrip() for page in checked.pages)

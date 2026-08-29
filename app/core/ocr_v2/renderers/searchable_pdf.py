"""Searchable-PDF boundary consuming actual canonical word geometry only."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..contracts import DocumentResult
from ..errors import RenderingNotEligibleError
from ..validation import OCRProfile, require_profile


class SearchablePdfRenderer:
    """Add an invisible text layer; this class never performs OCR."""

    def render(self, source_pdf: str | Path, result: DocumentResult, output_pdf: str | Path) -> None:
        try:
            checked = require_profile(result, OCRProfile.SEARCHABLE_PDF_V2)
        except Exception as exc:
            raise RenderingNotEligibleError("SEARCHABLE_PDF_V2 requires validated actual word geometry") from exc
        source = Path(source_pdf)
        target = Path(output_pdf)
        with fitz.open(str(source)) as document:
            if len(document) != len(checked.pages):
                raise RenderingNotEligibleError("source PDF and OCR result page counts differ")
            for page_result, page in zip(checked.pages, document):
                for token_id in page_result.reading_order:
                    token = next(token for token in page_result.tokens if token.id == token_id)
                    box = token.bbox
                    # render_mode=3 is invisible text. The position is derived
                    # from the canonical token box; no coordinates are invented.
                    page.insert_text((box.x, box.y + max(1.0, box.height * 0.85)), token.text, fontsize=max(1.0, min(12.0, box.height)), fontname="helv", render_mode=3, overlay=True)
            document.save(str(target), garbage=3, deflate=True)

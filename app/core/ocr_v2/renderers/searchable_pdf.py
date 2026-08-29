"""Searchable-PDF boundary consuming actual canonical word geometry only."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..contracts import DocumentResult
from ..errors import RenderingNotEligibleError
from ..validation import OCRProfile, require_profile
from .validation import validate_searchable_pdf_artifact


def _font_file_for_text(text: str) -> str | None:
    """Return an installed script font when one is available; never download."""
    import shutil
    import subprocess

    language = "ta" if any("\u0b80" <= char <= "\u0bff" for char in text) else "si" if any("\u0d80" <= char <= "\u0dff" for char in text) else ""
    if not language or shutil.which("fc-match") is None:
        return None
    try:
        path = subprocess.check_output(["fc-match", "-f", "%{file}", f":lang={language}"], text=True, timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return path if path and Path(path).is_file() else None


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
                    font_file = _font_file_for_text(token.text)
                    kwargs = {"fontname": "helv"}
                    if font_file:
                        # PyMuPDF keeps embedded fonts by alias.  Distinct
                        # script files must not share one alias (2H exposed
                        # cross-script Unicode loss when they did).
                        alias = "pdfnest_" + "".join(char if char.isalnum() else "_" for char in Path(font_file).stem)
                        kwargs = {"fontname": alias[:32], "fontfile": font_file}
                    page.insert_text((box.x, box.y + max(1.0, box.height * 0.85)), token.text, fontsize=max(1.0, min(12.0, box.height)), render_mode=3, overlay=True, **kwargs)
            document.save(str(target), garbage=3, deflate=True)
        validate_searchable_pdf_artifact(source, target, checked)

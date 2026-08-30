"""Searchable-PDF boundary consuming actual canonical word geometry only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz

from ..contracts import DocumentResult
from ..diagnostics import emit_searchable_diagnostic, safe_exception_message
from ..errors import RenderingNotEligibleError
from ..validation import OCRProfile, require_profile
from .validation import validate_searchable_pdf_artifact


def _font_file_for_text(text: str) -> str | None:
    """Return an installed script font when one is available; never download."""
    import shutil
    import subprocess

    language = "ta" if any("\u0b80" <= char <= "\u0bff" for char in text) else "si" if any("\u0d80" <= char <= "\u0dff" for char in text) else ""
    if not language:
        # Helvetica's built-in encoding does not cover every Unicode glyph
        # returned by OCR (for example U+20AC EURO SIGN).  Ask fontconfig for
        # a local font covering the actual non-ASCII characters in this token;
        # ASCII-only OCR remains on the compact built-in Helvetica path.
        codepoints = sorted({f"{ord(char):x}" for char in text if ord(char) > 0x7F})
        language = f":charset={','.join(codepoints)}" if codepoints else ""
    if not language or shutil.which("fc-match") is None:
        return None
    try:
        pattern = language if language.startswith(":charset=") else f":lang={language}"
        path = subprocess.check_output(["fc-match", "-f", "%{file}", pattern], text=True, timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return path if path and Path(path).is_file() else None


class SearchablePdfRenderer:
    """Add an invisible text layer; this class never performs OCR."""

    def render(
        self,
        source_pdf: str | Path,
        result: DocumentResult,
        output_pdf: str | Path,
        *,
        job_id: str | None = None,
    ) -> None:
        emit_searchable_diagnostic(event="RENDER_START", job_id=job_id, substage="PDF_RENDER_PROFILE_CHECK")
        try:
            checked = require_profile(result, OCRProfile.SEARCHABLE_PDF_V2)
        except Exception as exc:
            raise RenderingNotEligibleError(
                "SEARCHABLE_PDF_V2 requires validated actual word geometry",
                substage="PDF_RENDER_PROFILE_CHECK",
                reason_code="PROFILE_NOT_ELIGIBLE",
            ) from exc
        source = Path(source_pdf)
        target = Path(output_pdf)
        emit_searchable_diagnostic(event="RENDER_CHECK", job_id=job_id, substage="PDF_RENDER_SOURCE_SETUP", fields={"source_pdf": "normalized-image-pdf"})
        try:
            document = fitz.open(str(source))
        except Exception as exc:
            raise RenderingNotEligibleError("normalized source PDF could not be opened", substage="PDF_RENDER_SOURCE_SETUP", reason_code="SOURCE_PDF_INVALID") from exc
        try:
            if len(document) != len(checked.pages):
                raise RenderingNotEligibleError(
                    "source PDF and OCR result page counts differ",
                    substage="PDF_RENDER_SOURCE_SETUP",
                    reason_code="PAGE_COUNT_MISMATCH",
                )
            for page_result, page in zip(checked.pages, document):
                emit_searchable_diagnostic(
                    event="RENDER_CHECK",
                    job_id=job_id,
                    substage="PDF_RENDER_FONT_SELECTION",
                    fields={"page_index": page_result.page_index, "token_count": len(page_result.tokens)},
                )
                for token_id in page_result.reading_order:
                    token = next(token for token in page_result.tokens if token.id == token_id)
                    box = token.bbox
                    try:
                        # render_mode=3 is invisible text. The position is derived
                        # from the canonical token box; no coordinates are invented.
                        font_file = _font_file_for_text(token.text)
                        kwargs = {"fontname": "helv"}
                        if font_file:
                            # PyMuPDF keeps embedded fonts by alias. Distinct
                            # script files must not share one alias.
                            alias = "pdfnest_" + "".join(char if char.isalnum() else "_" for char in Path(font_file).stem)
                            kwargs = {"fontname": alias[:32], "fontfile": font_file}
                    except Exception as exc:
                        raise RenderingNotEligibleError(
                            "font selection failed for a canonical word",
                            substage="PDF_RENDER_FONT_SELECTION",
                            reason_code="FONT_SELECTION_FAILED",
                        ) from exc
                    try:
                        # Keep a separator in the invisible text stream.  OCR
                        # engines can return genuinely overlapping word boxes;
                        # without a separator PyMuPDF may merge adjacent
                        # insertions during extraction, losing the canonical
                        # reading-order boundary even though the visible page
                        # is unchanged.
                        page.insert_text((box.x, box.y + max(1.0, box.height * 0.85)), f"{token.text} ", fontsize=max(1.0, min(12.0, box.height)), render_mode=3, overlay=True, **kwargs)
                    except Exception as exc:
                        raise RenderingNotEligibleError(
                            "invisible text insertion failed for a canonical word",
                            substage="PDF_RENDER_TEXT_INSERTION",
                            reason_code="TEXT_INSERTION_FAILED",
                        ) from exc
                emit_searchable_diagnostic(
                    event="RENDER_CHECK",
                    job_id=job_id,
                    substage="PDF_RENDER_TEXT_INSERTION",
                    fields={"page_index": page_result.page_index, "inserted_word_count": len(page_result.reading_order)},
                )
            emit_searchable_diagnostic(event="RENDER_CHECK", job_id=job_id, substage="PDF_RENDER_SAVE")
            try:
                document.save(str(target), garbage=3, deflate=True)
            except Exception as exc:
                raise RenderingNotEligibleError("rendered PDF could not be finalized", substage="PDF_RENDER_SAVE", reason_code="PDF_SAVE_FAILED") from exc
        finally:
            document.close()

        _log_output_metadata(target, job_id=job_id)
        validate_searchable_pdf_artifact(source, target, checked, job_id=job_id)


def _log_output_metadata(output_pdf: Path, *, job_id: str | None) -> dict[str, Any] | None:
    """Record safe output facts before the independent validator runs."""
    try:
        data = output_pdf.read_bytes()
        metadata: dict[str, Any] = {
            "pdf_byte_length": len(data),
            "pdf_header_valid": data[:5] == b"%PDF-",
            "pages": [],
        }
        with fitz.open(str(output_pdf)) as document:
            metadata["page_count"] = len(document)
            for index, page in enumerate(document):
                image_info = page.get_image_info()
                metadata["pages"].append(
                    {
                        "page_index": index,
                        "width": page.rect.width,
                        "height": page.rect.height,
                        "image_count": len(page.get_images(full=True)),
                        "image_dimensions": [{"width": info.get("width"), "height": info.get("height")} for info in image_info],
                        "image_colorspaces": [info.get("cs-name") for info in image_info],
                        "extracted_word_count": len(page.get_text("words")),
                        "raster_width": page.get_pixmap(alpha=False).width,
                        "raster_height": page.get_pixmap(alpha=False).height,
                    }
                )
        emit_searchable_diagnostic(event="RENDER_OUTPUT_READY", job_id=job_id, substage="PDF_RENDER_SAVE", fields=metadata)
        return metadata
    except Exception as exc:
        emit_searchable_diagnostic(
            event="RENDER_OUTPUT_METADATA_FAILED",
            job_id=job_id,
            substage="PDF_RENDER_SAVE",
            fields={"exception_class": type(exc).__name__, "exception_message": safe_exception_message(exc)},
        )
        return None

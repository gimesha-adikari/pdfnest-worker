"""Independent validation for Searchable PDF V2 artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz

from ..contracts import DocumentResult
from ..errors import RenderingNotEligibleError
from ..diagnostics import emit_searchable_diagnostic


def _validation_error(
    substage: str,
    reason_code: str,
    message: str,
    *,
    job_id: str | None = None,
    page_index: int | None = None,
) -> RenderingNotEligibleError:
    fields = {"reason_code": reason_code}
    if page_index is not None:
        fields["page_index"] = page_index
    emit_searchable_diagnostic(event="VALIDATION_FAILURE", job_id=job_id, substage=substage, fields=fields)
    return RenderingNotEligibleError(message, substage=substage, reason_code=reason_code)


def validate_searchable_pdf_artifact(
    source_pdf: str | Path,
    output_pdf: str | Path,
    result: DocumentResult,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Validate the durable PDF with a reader independent of the renderer.

    The source is expected to be the normalized image-page PDF.  Visual pages
    must therefore remain pixel-equivalent while the output gains an invisible
    searchable text layer.
    """

    source_path = Path(source_pdf)
    output_path = Path(output_pdf)
    source = None
    output = None
    try:
        emit_searchable_diagnostic(event="VALIDATION_START", job_id=job_id, substage="PDF_VALIDATE_OPEN")
        source = fitz.open(str(source_path))
        output = fitz.open(str(output_path))
    except Exception as exc:
        if source is not None:
            source.close()
        raise _validation_error("PDF_VALIDATE_OPEN", "OUTPUT_PDF_INVALID", "searchable PDF artifact could not be opened", job_id=job_id) from exc

    try:
        emit_searchable_diagnostic(
            event="VALIDATION_CHECK",
            job_id=job_id,
            substage="PDF_VALIDATE_PAGE_COUNT",
            fields={"source_page_count": len(source), "output_page_count": len(output), "result_page_count": len(result.pages)},
        )
        if len(output) != len(result.pages) or len(source) != len(result.pages):
            raise _validation_error("PDF_VALIDATE_PAGE_COUNT", "PAGE_COUNT_MISMATCH", "artifact page count does not match source/result", job_id=job_id)

        extracted_words = 0
        for index, (source_page, output_page, page_result) in enumerate(zip(source, output, result.pages)):
            emit_searchable_diagnostic(
                event="VALIDATION_CHECK",
                job_id=job_id,
                substage="PDF_VALIDATE_PAGE_DIMENSIONS",
                fields={
                    "page_index": index,
                    "source_width": source_page.rect.width,
                    "source_height": source_page.rect.height,
                    "output_width": output_page.rect.width,
                    "output_height": output_page.rect.height,
                },
            )
            if abs(source_page.rect.width - output_page.rect.width) > 0.01 or abs(source_page.rect.height - output_page.rect.height) > 0.01:
                raise _validation_error("PDF_VALIDATE_PAGE_DIMENSIONS", "PAGE_DIMENSION_MISMATCH", f"artifact page {index} dimensions changed", job_id=job_id, page_index=index)

            try:
                images = output_page.get_images(full=True)
                source_image_info = source_page.get_image_info()
                output_image_info = output_page.get_image_info()
            except Exception as exc:
                raise _validation_error("PDF_VALIDATE_IMAGE_PRESENCE", "SOURCE_IMAGE_MISSING", f"artifact page {index} image inspection failed", job_id=job_id, page_index=index) from exc
            emit_searchable_diagnostic(
                event="VALIDATION_CHECK",
                job_id=job_id,
                substage="PDF_VALIDATE_IMAGE_PRESENCE",
                fields={
                    "page_index": index,
                    "source_image_count": len(source_image_info),
                    "output_image_count": len(images),
                    "source_image_dimensions": [{"width": info.get("width"), "height": info.get("height")} for info in source_image_info],
                    "output_image_dimensions": [{"width": info.get("width"), "height": info.get("height")} for info in output_image_info],
                },
            )
            if not images:
                raise _validation_error("PDF_VALIDATE_IMAGE_PRESENCE", "SOURCE_IMAGE_MISSING", f"artifact page {index} has no source image", job_id=job_id, page_index=index)

            try:
                words = output_page.get_text("words")
            except Exception as exc:
                raise _validation_error("PDF_VALIDATE_TEXT_EXTRACTION", "TEXT_EXTRACTION_FAILED", f"artifact page {index} text extraction failed", job_id=job_id, page_index=index) from exc
            extracted_words += len(words)
            expected = [page_result.tokens_by_id[token_id].text for token_id in page_result.reading_order] if page_result.tokens else []
            actual = [str(word[4]) for word in words]
            emit_searchable_diagnostic(
                event="VALIDATION_CHECK",
                job_id=job_id,
                substage="PDF_VALIDATE_TEXT_EXTRACTION",
                fields={"page_index": index, "extracted_word_count": len(actual), "expected_word_count": len(expected)},
            )
            if expected and not actual:
                raise _validation_error("PDF_VALIDATE_TEXT_EXTRACTION", "TEXT_EXTRACTION_MISMATCH", f"artifact page {index} text layer is empty", job_id=job_id, page_index=index)
            if expected and " ".join(expected) not in " ".join(actual):
                raise _validation_error("PDF_VALIDATE_READING_ORDER", "READING_ORDER_MISMATCH", f"artifact page {index} text layer is not extractable in reading order", job_id=job_id, page_index=index)

            try:
                source_pixmap = source_page.get_pixmap(alpha=False)
                output_pixmap = output_page.get_pixmap(alpha=False)
            except Exception as exc:
                raise _validation_error("PDF_VALIDATE_VISUAL_RASTER", "VISIBLE_RASTER_MISMATCH", f"artifact page {index} raster comparison failed", job_id=job_id, page_index=index) from exc
            emit_searchable_diagnostic(
                event="VALIDATION_CHECK",
                job_id=job_id,
                substage="PDF_VALIDATE_VISUAL_RASTER",
                fields={
                    "page_index": index,
                    "source_raster_width": source_pixmap.width,
                    "source_raster_height": source_pixmap.height,
                    "output_raster_width": output_pixmap.width,
                    "output_raster_height": output_pixmap.height,
                },
            )
            if (source_pixmap.width, source_pixmap.height) != (output_pixmap.width, output_pixmap.height) or source_pixmap.samples != output_pixmap.samples:
                raise _validation_error("PDF_VALIDATE_VISUAL_RASTER", "VISIBLE_RASTER_MISMATCH", f"artifact page {index} visible appearance changed", job_id=job_id, page_index=index)
        return {"page_count": len(output), "extracted_word_count": extracted_words, "visual_match": True}
    except RenderingNotEligibleError:
        raise
    except Exception as exc:
        raise _validation_error("PDF_VALIDATE_OPEN", "OUTPUT_PDF_INVALID", "searchable PDF artifact could not be independently read", job_id=job_id) from exc
    finally:
        if source is not None:
            source.close()
        if output is not None:
            output.close()

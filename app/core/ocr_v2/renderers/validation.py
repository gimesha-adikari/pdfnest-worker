"""Independent validation for Searchable PDF V2 artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz

from ..contracts import DocumentResult
from ..errors import RenderingNotEligibleError


def validate_searchable_pdf_artifact(
    source_pdf: str | Path,
    output_pdf: str | Path,
    result: DocumentResult,
) -> dict[str, Any]:
    """Validate the durable PDF with a reader independent of the renderer.

    The source is expected to be the normalized image-page PDF.  Visual pages
    must therefore remain pixel-equivalent while the output gains an invisible
    searchable text layer.
    """

    source_path = Path(source_pdf)
    output_path = Path(output_pdf)
    try:
        with fitz.open(str(source_path)) as source, fitz.open(str(output_path)) as output:
            if len(output) != len(result.pages) or len(source) != len(result.pages):
                raise RenderingNotEligibleError("artifact page count does not match source/result")
            extracted_words = 0
            for index, (source_page, output_page, page_result) in enumerate(zip(source, output, result.pages)):
                if abs(source_page.rect.width - output_page.rect.width) > 0.01 or abs(source_page.rect.height - output_page.rect.height) > 0.01:
                    raise RenderingNotEligibleError(f"artifact page {index} dimensions changed")
                if not output_page.get_images(full=True):
                    raise RenderingNotEligibleError(f"artifact page {index} has no source image")
                words = output_page.get_text("words")
                extracted_words += len(words)
                expected = [page_result.tokens_by_id[token_id].text for token_id in page_result.reading_order] if page_result.tokens else []
                actual = [str(word[4]) for word in words]
                if expected and " ".join(expected) not in " ".join(actual):
                    raise RenderingNotEligibleError(f"artifact page {index} text layer is not extractable in reading order")
                source_pixmap = source_page.get_pixmap(alpha=False)
                output_pixmap = output_page.get_pixmap(alpha=False)
                if (source_pixmap.width, source_pixmap.height) != (output_pixmap.width, output_pixmap.height) or source_pixmap.samples != output_pixmap.samples:
                    raise RenderingNotEligibleError(f"artifact page {index} visible appearance changed")
            return {"page_count": len(output), "extracted_word_count": extracted_words, "visual_match": True}
    except RenderingNotEligibleError:
        raise
    except Exception as exc:
        raise RenderingNotEligibleError("searchable PDF artifact could not be independently read") from exc

"""Project canonical OCR V2 results into the General Editor layout contract.

The projection is deliberately engine-neutral.  General Editor can therefore
use the frozen PDFNest worker or the standalone ``platen_document`` SDK without
loading the other implementation merely to translate the result.
"""

from __future__ import annotations

from typing import Any


def _value(value: object) -> object:
    return getattr(value, "value", value)


def editor_page_kind(classification: object) -> str:
    return {
        "TEXT_NATIVE": "text",
        "IMAGE_SCAN": "scanned",
        "MIXED": "mixed",
        "SUSPICIOUS_TEXT_LAYER": "scanned",
        "NEAR_BLANK": "blank",
        "BLANK": "blank",
    }.get(str(_value(classification)), "mixed")


def first_failed_editor_page(result: Any) -> Any | None:
    return next(
        (
            page
            for page in result.pages
            if str(_value(getattr(page, "status", None))) == "FAILED"
        ),
        None,
    )


def canonical_editor_elements(page_result: Any) -> list[dict[str, Any]]:
    """Adapt canonical OCR V2 words into the editor's line element contract."""

    tokens = page_result.tokens_by_id
    elements: list[dict[str, Any]] = []
    for index, line in enumerate(page_result.lines, start=1):
        words = [tokens[token_id] for token_id in line.token_ids if token_id in tokens]
        if not words and not line.text.strip():
            continue
        rect = line.bbox
        heights = [word.bbox.height for word in words if word.bbox.height > 0]
        size = max(8.0, (max(heights) if heights else rect.height) * 1.15)
        elements.append(
            {
                "id": f"p{page_result.page_index + 1}-ocr-v2-line-{index}",
                "text": line.text,
                "original_text": line.text,
                "x": rect.x,
                "y": rect.y,
                "width": rect.width,
                "height": rect.height,
                "size": round(size, 1),
                "font": "tiro",
                "bg_color": "transparent",
                "text_color": "#000000",
                "transparent_bg": True,
                "ocr_v2": True,
                "source": _value(page_result.processing_source),
                "provenance": list(page_result.provenance_refs),
                "word_ids": [word.id for word in words],
                "word_geometry": [
                    {
                        "id": word.id,
                        "text": word.text,
                        "x": word.bbox.x,
                        "y": word.bbox.y,
                        "width": word.bbox.width,
                        "height": word.bbox.height,
                    }
                    for word in words
                ],
                "reading_order": [word.id for word in words],
                "confidence": sum(
                    word.confidence.raw_value for word in words if word.confidence
                )
                / max(1, sum(1 for word in words if word.confidence)),
            }
        )
    return elements


def project_editor_result(result: Any) -> dict[str, Any]:
    """Return the stable ``ocr_v2_editor_layout.v1`` result projection."""

    pages: list[dict[str, Any]] = []
    for page in result.pages:
        elements = canonical_editor_elements(page)
        classification = str(_value(page.content_classification))
        processing_source = _value(page.processing_source)
        pages.append(
            {
                "page_num": page.page_index + 1,
                "width": page.geometry.width,
                "height": page.geometry.height,
                "elements": elements,
                "kind": editor_page_kind(classification),
                "is_ocr": processing_source == "OCR_RECOGNITION",
                "has_selectable_text": bool(page.tokens),
                "word_count": len(page.tokens),
                "text_block_count": len(page.lines),
                "image_block_count": 1 if classification in {"IMAGE_SCAN", "MIXED"} else 0,
                "source": processing_source,
                "provenance": list(page.provenance_refs),
                "reading_order": list(page.reading_order),
                "capabilities": sorted(page.capabilities),
            }
        )

    source = (
        result.source.to_dict()
        if hasattr(result.source, "to_dict")
        else {
            "page_count": result.source.page_count,
            "filename": result.source.filename,
        }
    )
    return {
        "success": True,
        "schema_version": "ocr_v2_editor_layout.v1",
        "ocr_v2": True,
        "pages": pages,
        "source": source,
    }


__all__ = [
    "canonical_editor_elements",
    "editor_page_kind",
    "first_failed_editor_page",
    "project_editor_result",
]

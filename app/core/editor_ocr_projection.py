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


def _studio_native_dimensions(page_result: Any) -> tuple[float, float]:
    """Return the unrotated CropBox width/height for a native SDK page.

    ``PageGeometry`` exposes the visible page dimensions.  A quarter-turn
    therefore swaps the visible width/height relative to the unrotated PDF
    coordinate system in which the SDK native boxes are reported.
    """

    geometry = page_result.geometry
    rotation = int(getattr(geometry, "rotation", 0) or 0) % 360
    if rotation in (90, 270):
        return float(geometry.height), float(geometry.width)
    return float(geometry.width), float(geometry.height)


def _studio_visible_rect(rect: dict[str, Any], page_result: Any) -> dict[str, Any]:
    """Map one canonical native rectangle into Studio visible page space."""

    x = float(rect["x"])
    y = float(rect["y"])
    width = float(rect["width"])
    height = float(rect["height"])
    page_width, page_height = _studio_native_dimensions(page_result)
    rotation = int(getattr(page_result.geometry, "rotation", 0) or 0) % 360

    if rotation == 90:
        visible = (page_height - y - height, x, height, width)
    elif rotation == 180:
        visible = (page_width - x - width, page_height - y - height, width, height)
    elif rotation == 270:
        visible = (y, page_width - x - width, height, width)
    else:
        visible = (x, y, width, height)

    projected = dict(rect)
    projected["x"], projected["y"], projected["width"], projected["height"] = visible
    return projected


def _project_studio_native_elements(elements: list[dict[str, Any]], page_result: Any) -> None:
    """Transform every geometry-bearing field in the editor element contract."""

    geometry_collections = (
        "word_geometry",
        "line_geometry",
        "block_geometry",
        "selection_geometry",
        "selection_boxes",
        "original_text_geometry",
    )
    for element in elements:
        element.update(_studio_visible_rect(element, page_result))
        for key in geometry_collections:
            value = element.get(key)
            if not isinstance(value, list):
                continue
            element[key] = [
                _studio_visible_rect(item, page_result)
                if isinstance(item, dict) and {"x", "y", "width", "height"}.issubset(item)
                else item
                for item in value
            ]


def project_studio_editor_result(result: Any) -> dict[str, Any]:
    """Project an SDK result into Studio's visible editor layout contract.

    The public SDK deliberately keeps native PDF text geometry canonical so
    callers such as the General Editor compiler can pass it directly to
    PyMuPDF.  Studio's canvas, however, positions overlays directly against
    the visibly rotated CropBox and has no downstream transform.  Native SDK
    pages are therefore mapped here, at the Studio adapter boundary.  OCR
    pages already use rendered visible coordinates and are copied unchanged.
    """

    projected = project_editor_result(result)
    for page_result, page in zip(result.pages, projected["pages"]):
        source = str(_value(getattr(page_result, "processing_source", None)))
        if source != "NATIVE_EXTRACTION":
            continue
        _project_studio_native_elements(page["elements"], page_result)

    return projected


__all__ = [
    "canonical_editor_elements",
    "editor_page_kind",
    "first_failed_editor_page",
    "project_editor_result",
    "project_studio_editor_result",
]

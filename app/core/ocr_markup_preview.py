"""Safe OCR word-geometry projection for the standalone markup preview.

This module does not recognize text or mutate PDFs.  It projects the already
validated canonical OCR result into the small, temporary contract needed by the
authenticated markup workspace.  PDFNest owns the request and document
lifecycle; the selected OCR engine owns the canonical result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _page_kind(classification: object) -> str:
    return {
        "TEXT_NATIVE": "native",
        "IMAGE_SCAN": "scanned",
        "MIXED": "mixed",
        "SUSPICIOUS_TEXT_LAYER": "scanned",
        "NEAR_BLANK": "blank",
        "BLANK": "blank",
    }.get(_value(classification), "mixed")


def _crop_box(page: Any) -> list[float] | None:
    crop = getattr(page, "cropbox", None)
    if crop is None:
        return None
    return [float(crop.x0), float(crop.y0), float(crop.x1), float(crop.y1)]


def project_markup_preview(result: Any, input_path: str | Path) -> dict[str, Any]:
    """Return only page geometry and reading-order words for browser selection."""

    pages: list[dict[str, Any]] = []
    with fitz.open(str(input_path)) as document:
        for page_result in result.pages:
            tokens = page_result.tokens_by_id
            words: list[dict[str, Any]] = []
            reading_order: list[str] = []
            for order, token_id in enumerate(page_result.reading_order):
                token = tokens.get(token_id)
                if token is None or not token.text.strip():
                    continue
                reading_order.append(token.id)
                words.append(
                    {
                        "id": token.id,
                        "text": token.text,
                        "x": float(token.bbox.x),
                        "y": float(token.bbox.y),
                        "width": float(token.bbox.width),
                        "height": float(token.bbox.height),
                        "order": order,
                        "confidence": float(token.confidence.raw_value) if token.confidence else None,
                    }
                )

            source_page = document[page_result.page_index]
            classification = _value(page_result.content_classification)
            pages.append(
                {
                    "page_index": page_result.page_index,
                    "page_number": page_result.page_index + 1,
                    "page_id": page_result.page_id,
                    "width": float(page_result.geometry.width),
                    "height": float(page_result.geometry.height),
                    "rotation": int(page_result.geometry.rotation),
                    "coordinate_space": page_result.geometry.coordinate_space,
                    "crop_box": _crop_box(source_page),
                    "classification": classification,
                    "kind": _page_kind(classification),
                    "selection_mode": "native" if classification == "TEXT_NATIVE" else "ocr",
                    "status": _value(page_result.status),
                    "has_selectable_text": bool(words),
                    "word_count": len(words),
                    "reading_order": reading_order,
                    "words": words,
                    "language": {
                        "requested": list(page_result.language.requested_languages),
                        "detected": list(page_result.language.detected_languages),
                        "status": page_result.language.language_status,
                        "mode": page_result.language.requested_mode,
                    },
                }
            )

    return {
        "schema_version": "ocr_v2_markup_preview.v1",
        "profile": "MARKUP_V2",
        "status": "SUCCEEDED",
        "page_count": len(pages),
        "pages": pages,
    }


__all__ = ["project_markup_preview"]

from __future__ import annotations

from typing import Any

from .utils import open_document, rect_area


def analyze_page(page, page_number: int) -> dict[str, Any]:
    page_area = max(1.0, page.rect.width * page.rect.height)

    words = page.get_text("words") or []
    text_dict = page.get_text("dict") or {}

    blocks = text_dict.get("blocks", [])

    word_count = len(words)

    text_block_count = 0
    image_block_count = 0

    text_block_area = 0.0
    image_block_area = 0.0

    for block in blocks:
        if not isinstance(block, dict):
            continue

        bbox = block.get("bbox")
        if bbox is None:
            continue

        area = rect_area(bbox)

        if block.get("type") == 0:
            text_block_count += 1
            text_block_area += area

        elif block.get("type") == 1:
            image_block_count += 1
            image_block_area += area

    text_area_ratio = min(1.0, text_block_area / page_area)
    image_area_ratio = min(1.0, image_block_area / page_area)

    has_selectable_text = word_count > 0

    if word_count == 0 and image_block_count == 0:
        kind = "blank"

    elif word_count == 0:
        kind = "scanned"

    elif image_block_count == 0:
        kind = "text"

    else:
        if image_area_ratio >= 0.50 and text_area_ratio <= 0.12:
            kind = "mixed"
        elif image_area_ratio >= 0.15 and text_area_ratio >= 0.01:
            kind = "mixed"
        else:
            kind = "text"

    return {
        "page": page_number,
        "kind": kind,
        "hasSelectableText": has_selectable_text,
        "wordCount": word_count,
        "textBlockCount": text_block_count,
        "imageBlockCount": image_block_count,
        "textAreaRatio": round(text_area_ratio, 6),
        "imageAreaRatio": round(image_area_ratio, 6),
    }


def analyze_document(input_path: str, password: str | None = None):
    doc = open_document(input_path, password)

    try:
        pages = []

        for i in range(doc.page_count):
            pages.append(analyze_page(doc[i], i + 1))

        return {
            "pageCount": doc.page_count,
            "pages": pages,
        }

    finally:
        doc.close()
"""Native PDF text extraction and conservative page classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contracts import PageContentClassification, PageGeometry


class NativeDecision(str):
    TRUST_NATIVE = "TRUST_NATIVE"
    VISUAL_OCR_REQUIRED = "VISUAL_OCR_REQUIRED"
    MIXED_OR_REGION_REVIEW = "MIXED_OR_REGION_REVIEW"
    UNDECIDED = "UNDECIDED"


@dataclass(frozen=True)
class NativeExtractionCandidate:
    page_index: int
    page_id: str
    text: str
    items: tuple[dict[str, Any], ...]
    image_count: int
    has_full_page_image: bool
    suspicious_text: bool
    geometry: PageGeometry


@dataclass(frozen=True)
class NativeValidationResult:
    decision: str
    classification: PageContentClassification
    reasons: tuple[str, ...] = ()


_SUSPICIOUS_RE = re.compile(r"[\ufffd\u200b\u200c\u200d]")


class NativeExtractor:
    def extract(self, page: Any, page_index: int) -> NativeExtractionCandidate:
        words = page.get_text("words") or []
        items: list[dict[str, Any]] = []
        page_width = max(float(page.rect.width), 0.0)
        page_height = max(float(page.rect.height), 0.0)
        for index, word in enumerate(words):
            if len(word) < 5 or not str(word[4]).strip():
                continue
            # PyMuPDF can return glyph boxes that extend by a few points past
            # the visible crop box when text is positioned at an edge. Keep
            # the extractor's real geometry, clipped to the visible page
            # contract, instead of allowing one edge glyph to invalidate an
            # otherwise trustworthy native-text result.
            x0 = min(max(float(word[0]), 0.0), page_width)
            y0 = min(max(float(word[1]), 0.0), page_height)
            x1 = min(max(float(word[2]), x0), page_width)
            y1 = min(max(float(word[3]), y0), page_height)
            if x1 <= x0 or y1 <= y0:
                continue
            items.append(
                {
                    "id": f"native-{page_index}-{index}",
                    "text": str(word[4]),
                    "bbox": [x0, y0, x1, y1],
                    "block_id": str(word[5]) if len(word) > 5 else None,
                    "line_id": f"{word[5] if len(word) > 5 else 0}:{word[6] if len(word) > 6 else 0}",
                }
            )
        text = str(page.get_text("text") or "")
        images = page.get_images(full=True) or []
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        full_page = False
        for image in images:
            try:
                rects = page.get_image_rects(image)
                full_page = full_page or any((rect.width * rect.height) / page_area >= 0.95 for rect in rects)
            except Exception:
                continue
        return NativeExtractionCandidate(
            page_index=page_index,
            page_id=f"page-{page_index}",
            text=text,
            items=tuple(items),
            image_count=len(images),
            has_full_page_image=full_page,
            suspicious_text=bool(_SUSPICIOUS_RE.search(text)),
            geometry=PageGeometry(float(page.rect.width), float(page.rect.height), int(getattr(page, "rotation", 0) or 0)),
        )


class NativeValidator:
    def validate(self, candidate: NativeExtractionCandidate) -> NativeValidationResult:
        text = candidate.text.strip()
        if not text:
            if candidate.image_count:
                return NativeValidationResult(
                    NativeDecision.VISUAL_OCR_REQUIRED,
                    PageContentClassification.IMAGE_SCAN,
                    ("NO_NATIVE_TEXT", "IMAGE_CONTENT_PRESENT"),
                )
            return NativeValidationResult(NativeDecision.TRUST_NATIVE, PageContentClassification.BLANK, ("NO_CONTENT",))
        if len(text) <= 3 and candidate.image_count:
            return NativeValidationResult(
                NativeDecision.VISUAL_OCR_REQUIRED,
                PageContentClassification.NEAR_BLANK,
                ("NEAR_BLANK_TEXT", "IMAGE_CONTENT_PRESENT"),
            )
        if candidate.suspicious_text:
            return NativeValidationResult(
                NativeDecision.VISUAL_OCR_REQUIRED,
                PageContentClassification.SUSPICIOUS_TEXT_LAYER,
                ("SUSPICIOUS_UNICODE",),
            )
        if candidate.has_full_page_image:
            return NativeValidationResult(
                NativeDecision.MIXED_OR_REGION_REVIEW,
                PageContentClassification.MIXED,
                ("FULL_PAGE_IMAGE_WITH_NATIVE_TEXT",),
            )
        if candidate.image_count:
            return NativeValidationResult(
                NativeDecision.MIXED_OR_REGION_REVIEW,
                PageContentClassification.MIXED,
                ("NATIVE_TEXT_AND_IMAGES",),
            )
        return NativeValidationResult(NativeDecision.TRUST_NATIVE, PageContentClassification.TEXT_NATIVE, ("NATIVE_TEXT_VALIDATED",))

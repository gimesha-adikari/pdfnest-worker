from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

import fitz

from app.api.tools.markdown.ir import BlockSource, ContentRole, IRNode, Rect

WATERMARK_KEYWORDS = {"CONFIDENTIAL", "DRAFT", "SAMPLE", "COPY", "INTERNAL USE ONLY", "VOID", "WATERMARK"}
PAGE_NUMBER_REGEX = re.compile(r"^(Page\s+\d+(\s+of\s+\d+)?|\d+/\d+|\d+)$", re.IGNORECASE)


def classify_page_content(
    page: fitz.Page, page_width: float, page_height: float
) -> str:
    """
    Multi-signal page content classifier.
    Returns: 'NATIVE', 'SCANNED', 'MIXED', 'EMPTY', or 'UNCERTAIN'.
    """
    native_text = page.get_text("text") or ""
    c_native = len(native_text.strip())

    page_area = page_width * page_height if page_width > 0 and page_height > 0 else 1.0

    images = page.get_images()
    total_img_area = 0.0
    for img in images:
        rects = page.get_image_rects(img[0])
        for r in rects:
            total_img_area += (r.x1 - r.x0) * (r.y1 - r.y0)

    a_img = total_img_area / page_area

    text_blocks = page.get_text("blocks")
    total_text_area = 0.0
    for b in text_blocks:
        total_text_area += (b[2] - b[0]) * (b[3] - b[1])
    a_text = total_text_area / page_area

    if c_native == 0 and a_img <= 0.05:
        return "EMPTY"
    elif c_native < 15 and a_img > 0.45:
        return "SCANNED"
    elif c_native >= 15 and a_img > 0.40 and (a_text * a_img) > 0.10:
        return "MIXED"
    elif c_native > 0:
        return "NATIVE"
    else:
        return "UNCERTAIN"


def is_multi_signal_watermark(
    node: IRNode,
    text: str,
    page_height: float,
    page_width: float,
    is_rotated: bool = False,
    font_size: float = 10.0,
    base_font_size: float = 10.0,
    opacity: float = 1.0,
    is_repeated_across_pages: bool = False,
) -> bool:
    """
    Multi-signal weighted scoring model for watermark classification.
    Requires combined evidence (score >= 3.5) to avoid false positives on rotated headings or body text.
    """
    clean_text = text.strip().upper()
    if not clean_text or page_height <= 0 or page_width <= 0:
        return False

    score = 0.0

    # Signal 1: Non-standard rotation angle (e.g. diagonal background text)
    if is_rotated:
        score += 2.0

    # Signal 2: Text opacity < 0.60
    if opacity < 0.60:
        score += 2.0

    # Signal 3: Watermark keyword match
    if any(kw in clean_text for kw in WATERMARK_KEYWORDS):
        score += 2.0

    # Signal 4: Large text scale relative to document base font size (>= 1.4x)
    ratio = font_size / base_font_size if base_font_size > 0 else 1.0
    if ratio >= 1.4:
        score += 1.5

    # Signal 5: Central page placement
    cx = (node.bbox.x0 + node.bbox.x1) / 2.0
    cy = (node.bbox.y0 + node.bbox.y1) / 2.0
    if (0.20 * page_width <= cx <= 0.80 * page_width) and (0.25 * page_height <= cy <= 0.75 * page_height):
        score += 1.5

    # Signal 6: Repeated background text across document
    if is_repeated_across_pages:
        score += 2.0

    # Requires combined evidence (score >= 3.5)
    return score >= 3.5


def classify_node_content_role(
    node: IRNode,
    text: str,
    page_height: float,
    page_width: float,
    repeated_headers: Set[str],
    repeated_footers: Set[str],
    is_rotated: bool = False,
    font_size: float = 10.0,
    base_font_size: float = 10.0,
    opacity: float = 1.0,
    is_repeated_across_pages: bool = False,
) -> ContentRole:
    """Classifies a node's content role using multi-signal spatial, text, and structural evidence."""
    clean_text = text.strip()
    if not clean_text or page_height <= 0:
        return ContentRole.PRIMARY

    y0 = node.bbox.y0
    y1 = node.bbox.y1

    # Check multi-signal watermark
    if is_multi_signal_watermark(
        node, text, page_height, page_width, is_rotated, font_size, base_font_size, opacity, is_repeated_across_pages
    ):
        return ContentRole.WATERMARK

    # Running header check (Must repeat across >= 2 pages in repeated_headers)
    if y1 <= 0.08 * page_height and clean_text in repeated_headers:
        return ContentRole.HEADER

    # Running footer & page number check
    if y0 >= 0.90 * page_height:
        if PAGE_NUMBER_REGEX.match(clean_text):
            return ContentRole.PAGE_NUMBER
        elif clean_text in repeated_footers:
            return ContentRole.FOOTER

    return ContentRole.PRIMARY


def compute_string_similarity(s1: str, s2: str) -> float:
    """Calculates normalized SequenceMatcher similarity ratio between two strings."""
    return SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()


def reconcile_mixed_page_nodes(
    native_nodes: List[IRNode],
    ocr_nodes: List[IRNode],
) -> List[IRNode]:
    """
    Role-aware spatial reconciliation model for MIXED pages.
    Reconciles native vector spans with OCR lines based on IoU, text similarity, and content roles.
    """
    if not native_nodes:
        return ocr_nodes
    if not ocr_nodes:
        return native_nodes

    surviving_ocr_nodes: List[IRNode] = []

    for ocr_node in ocr_nodes:
        should_keep_ocr = True
        ocr_text = getattr(ocr_node, "raw_text", "")

        for native_node in native_nodes:
            iou = ocr_node.bbox.iou(native_node.bbox)
            native_text = getattr(native_node, "raw_text", "")
            sim = compute_string_similarity(ocr_text, native_text)

            # Rule 1: Watermarks, Annotations, Headers, and Footers NEVER suppress underlying document OCR text
            if native_node.role in (
                ContentRole.WATERMARK,
                ContentRole.ANNOTATION,
                ContentRole.HEADER,
                ContentRole.FOOTER,
            ):
                continue

            # Rule 2: PRIMARY native text + equivalent OCR -> prefer native, suppress duplicate OCR
            if native_node.role == ContentRole.PRIMARY:
                if sim >= 0.70 or (iou >= 0.50 and sim >= 0.40):
                    should_keep_ocr = False
                    break

        if should_keep_ocr:
            surviving_ocr_nodes.append(ocr_node)

    return native_nodes + surviving_ocr_nodes

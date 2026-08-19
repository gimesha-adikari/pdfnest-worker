from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, List, Optional, Tuple, Dict

import fitz

from app.api.tools.markdown.ir import (
    BlockSource,
    ContentRole,
    HeadingNode,
    IRNode,
    ListItemNode,
    ListNode,
    ParagraphNode,
    Rect,
    TextSpan,
)

BULLET_GLYPH_MAP = {
    "\u2022": "•",
    "\u25cf": "•",
    "\u25cb": "•",
    "\u25a0": "•",
    "\u2013": "-",
    "\u2014": "-",
    "(cid:127)": "•",
    "(cid:128)": "•",
    "(cid:133)": "…",
}

BULLET_REGEX = re.compile(r"^([\u2022\u25cf\u25cb\u25a0\u2013\u2014\-\*•]|(cid:\d+))\s*")
NUMBERED_LIST_REGEX = re.compile(
    r"^((\d+|[a-zA-Z]|[ivxIVX]+)[\.\)]|\(\d+\))\s+"
)
NUMBERED_HEADING_REGEX = re.compile(
    r"^(\d+(\.\d+)*|SECTION\s+[A-Z0-9]+|CHAPTER\s+[A-Z0-9]+)\.?\s+[A-Z]"
)


def calculate_document_base_font_size(doc: fitz.Document) -> float:
    """Calculates the character-weighted mode font size across all pages in document."""
    font_counter: Counter[float] = Counter()
    for page in doc:
        text_page = page.get_text("dict")
        for block in text_page.get("blocks", []):
            if block.get("type") == 0:  # Text block
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        size = round(float(span.get("size", 10.0)), 1)
                        if text:
                            font_counter[size] += len(text)
    if not font_counter:
        return 10.0
    return font_counter.most_common(1)[0][0]


def normalize_glyph_text(text: str) -> str:
    """Fixes broken PDF bullet and control character glyph encodings."""
    for glyph, replacement in BULLET_GLYPH_MAP.items():
        text = text.replace(glyph, replacement)
    return text


def is_bold_font(font_flags: int, font_name: str) -> bool:
    """Determines if a font span is bold from PyMuPDF flags and font name string."""
    if font_flags & 2 or "bold" in font_name.lower() or "black" in font_name.lower() or "heavy" in font_name.lower():
        return True
    return False


def is_italic_font(font_flags: int, font_name: str) -> bool:
    """Determines if a font span is italic/oblique."""
    if font_flags & 1 or "italic" in font_name.lower() or "oblique" in font_name.lower():
        return True
    return False


def classify_heading_level(
    span_size: float,
    base_size: float,
    is_bold: bool,
    text: str,
    is_isolated: bool,
) -> Optional[int]:
    """
    Multi-signal deterministic heading detection heuristic.
    Returns heading level 1-6 or None if paragraph text.
    """
    clean_text = text.strip()
    if not clean_text:
        return None

    # Lines ending with period, comma, or semicolon are body sentences, not headings
    if clean_text.endswith((".", ",", ";")):
        return None

    # Key-value label pattern ("Frontend: JavaScript...", "Backend: Java...") is NOT a heading
    if ":" in clean_text:
        prefix = clean_text.split(":", 1)[0].strip()
        if len(prefix) < 35 and len(clean_text) > len(prefix) + 2:
            return None

    ratio = span_size / base_size if base_size > 0 else 1.0
    is_uppercase = clean_text.isupper() and len(clean_text) >= 3

    # Match explicit numbered heading patterns (e.g., "1.2 Introduction")
    is_numbered = bool(NUMBERED_HEADING_REGEX.match(clean_text))

    if ratio >= 1.70 or (ratio >= 1.45 and (is_bold or is_uppercase)):
        return 1
    elif ratio >= 1.35 or (ratio >= 1.20 and (is_bold or is_uppercase)):
        return 2
    elif ratio >= 1.20 or (ratio >= 1.12 and is_bold and is_uppercase):
        return 3
    elif is_uppercase and is_bold and is_isolated and len(clean_text) < 40:
        return 2
    elif is_numbered and len(clean_text) < 80:
        return 3
    return None


def extract_page_links(page: fitz.Page) -> List[Dict[str, Any]]:
    """Extracts hyperlink annotations from a PyMuPDF page with bounding boxes and targets."""
    links = []
    for link in page.get_links():
        uri = link.get("uri")
        rect = link.get("from")
        if uri and rect:
            links.append({
                "url": uri,
                "bbox": Rect(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)
            })
    return links


def associate_link_with_span(span_bbox: Rect, page_links: List[Dict[str, Any]]) -> Optional[str]:
    """Finds if a text span overlaps with a link annotation bounding box."""
    for link in page_links:
        if span_bbox.intersects(link["bbox"]):
            return link["url"]
    return None

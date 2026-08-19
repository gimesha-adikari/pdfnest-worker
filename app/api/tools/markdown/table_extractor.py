# Adapted from Microsoft MarkItDown (MIT License)
# Upstream Source: https://github.com/microsoft/markitdown
# Origin File: markitdown/converters/_pdf_converter.py
# Provenance: Adapted for PDFNest DocumentIR table extraction

from __future__ import annotations

import logging
from typing import Any, List, Optional

import fitz

from app.api.tools.markdown.ir import (
    BlockSource,
    ContentRole,
    Rect,
    TableCellNode,
    TableNode,
)

logger = logging.getLogger(__name__)

SECTION_KEYWORDS = {
    "SUMMARY",
    "EDUCATION",
    "SKILLS",
    "PROJECTS",
    "EXPERIENCE",
    "WORK EXPERIENCE",
    "LEADERSHIP",
    "LANGUAGES",
}


def is_table_candidate_page(page: fitz.Page) -> bool:
    """
    Detects if page contains explicit candidate table grid drawings
    (intersecting horizontal and vertical lines forming enclosed cells).
    """
    drawings = page.get_drawings()
    h_lines = 0
    v_lines = 0

    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l":  # line (p1, p2)
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2.0 and abs(p1.x - p2.x) > 15.0:
                    h_lines += 1
                elif abs(p1.x - p2.x) < 2.0 and abs(p1.y - p2.y) > 15.0:
                    v_lines += 1

    return h_lines >= 2 and v_lines >= 2


def extract_table_nodes_from_pdfplumber(
    pdfplumber_page: Any, page_num: int, bbox_mask: Optional[Rect] = None
) -> List[TableNode]:
    """
    Evidence-based table extraction using pdfplumber's geometric table finder.
    Validates grid cell density and rejects false-positive layout containers
    (such as multi-column CVs and page-spanning border boxes).
    """
    try:
        tables = pdfplumber_page.find_tables()
    except Exception as e:
        logger.warning(f"pdfplumber find_tables failed on page {page_num}: {e}")
        return []

    if not tables:
        return []

    page_width = float(pdfplumber_page.width)
    page_height = float(pdfplumber_page.height)

    valid_tables: List[TableNode] = []

    for t_idx, t in enumerate(tables):
        x0, y0, x1, y1 = t.bbox
        width = x1 - x0
        height = y1 - y0

        # Rule 1: Reject out-of-bound bounding boxes
        if x0 < -5.0 or y0 < -5.0 or x1 > page_width + 10.0 or y1 > page_height + 10.0:
            logger.info(f"Page {page_num}: Rejecting table {t_idx} (out of page bounds {t.bbox})")
            continue

        # Rule 2: Reject page-spanning layout containers
        if width > 0.65 * page_width and height > 0.65 * page_height:
            logger.info(f"Page {page_num}: Rejecting table {t_idx} (page-spanning layout container width={width:.1f}, height={height:.1f})")
            continue

        extracted_rows = t.extract()
        if not extracted_rows or len(extracted_rows) < 2:
            continue

        # Rule 3: Check for section headers or prose paragraphs inside table cells
        cell_texts: List[str] = []
        has_section_header = False
        has_long_prose = False

        for r in extracted_rows:
            for cell in r:
                if cell:
                    c_str = str(cell).strip()
                    if c_str:
                        cell_texts.append(c_str)
                        if len(c_str) > 150 or c_str.count("\n") >= 3:
                            has_long_prose = True
                        for kw in SECTION_KEYWORDS:
                            if kw in c_str:
                                has_section_header = True

        if has_section_header or has_long_prose:
            logger.info(f"Page {page_num}: Rejecting table {t_idx} (contains section header={has_section_header} or long prose={has_long_prose})")
            continue

        # Rule 4: Clean cell text and build TableNode
        header_cells = [TableCellNode(text=str(c or "").strip(), colspan=1, rowspan=1) for c in extracted_rows[0]]
        body_rows = []
        for r in extracted_rows[1:]:
            body_rows.append([TableCellNode(text=str(c or "").strip(), colspan=1, rowspan=1) for c in r])

        num_cols = len(header_cells)
        if num_cols < 2:
            continue

        table_node = TableNode(
            bbox=Rect(x0=max(0.0, x0), y0=max(0.0, y0), x1=min(page_width, x1), y1=min(page_height, y1)),
            page_number=page_num,
            source=BlockSource.TABLE_EXTRACTOR,
            role=ContentRole.PRIMARY,
            headers=header_cells,
            rows=body_rows,
            alignments=["left"] * num_cols,
            upstream_origin="pdfplumber geometric table",
            confidence=0.90,
        )
        valid_tables.append(table_node)

    return valid_tables

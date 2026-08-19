from __future__ import annotations

import base64
import logging
from typing import Callable, Optional

import fitz
import pdfplumber

from app.api.tools.markdown.semantic_grouping import SemanticGrouper
from app.api.tools.markdown.classifier import (
    classify_node_content_role,
    classify_page_content,
    reconcile_mixed_page_nodes,
)

from app.api.tools.markdown.extractor import (
    associate_link_with_span,
    calculate_document_base_font_size,
    classify_heading_level,
    extract_page_links,
    is_bold_font,
    is_italic_font,
    normalize_glyph_text,
    NUMBERED_LIST_REGEX,
    BULLET_REGEX,
)

from app.api.tools.markdown.ir import (
    BlockSource,
    ContentRole,
    DocumentIR,
    HeadingNode,
    ImageNode,
    IRNode,
    ListItemNode,
    ListNode,
    PageBreakNode,
    PageNode,
    ParagraphNode,
    Rect,
    TextSpan,
)

from app.api.tools.markdown.layout import sort_nodes_in_topological_reading_order
from app.api.tools.markdown.renderer import MarkdownRenderer
from app.api.tools.markdown.table_extractor import (
    extract_table_nodes_from_pdfplumber,
    is_table_candidate_page,
)

from app.core.ocr_engine import OCREngine, validate_ocr_language

logger = logging.getLogger(__name__)


def convert_pdf_to_markdown(
    input_path: str,
    password: Optional[str] = None,
    lang: str = "eng",
    include_annotations: bool = False,
    embed_images: bool = False,
    cancellation_check: Optional[Callable[[], None]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    End-to-end PDF to Markdown converter service.
    Constructs DocumentIR through PyMuPDF, table clustering, and OCR fallback,
    and returns clean GitHub-Flavored Markdown.
    """
    # 1. Validate requested OCR language model upfront
    validate_ocr_language(lang)

    if progress_cb:
        progress_cb(10, "Opening PDF document")

    doc = fitz.open(input_path)

    # 2. Encrypted PDF handling
    if doc.is_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            raise fitz.FileDataError("Encrypted PDF requires a valid password for authentication.")

    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        return ""

    base_font_size = calculate_document_base_font_size(doc)

    # 3. Pre-scan running headers and footers across pages (Requires repetition across >= 2 pages)
    repeated_headers: set[str] = set()
    repeated_footers: set[str] = set()
    header_candidates: dict[str, int] = {}
    footer_candidates: dict[str, int] = {}

    for page in doc:
        blocks = page.get_text("blocks")
        p_height = page.rect.height
        for b in blocks:
            if len(b) >= 5:
                text = b[4].strip()
                if not text:
                    continue
                if b[3] <= 0.08 * p_height:
                    header_candidates[text] = header_candidates.get(text, 0) + 1
                elif b[1] >= 0.90 * p_height:
                    footer_candidates[text] = footer_candidates.get(text, 0) + 1

    for text, count in header_candidates.items():
        if count >= 2:
            repeated_headers.add(text)
    for text, count in footer_candidates.items():
        if count >= 2:
            repeated_footers.add(text)

    ir_pages = []

    plumber_doc = None
    try:
        plumber_doc = pdfplumber.open(input_path, password=password)
    except Exception as e:
        logger.debug(f"pdfplumber open failed: {e}")

    try:
        for page_idx in range(total_pages):
            if cancellation_check:
                cancellation_check()

            if progress_cb:
                pct = 15 + int((page_idx / total_pages) * 70)
                progress_cb(pct, f"Processing page {page_idx + 1} of {total_pages}")

            fitz_page = doc[page_idx]
            p_width = fitz_page.rect.width
            p_height = fitz_page.rect.height
            page_node = PageNode(
                page_number=page_idx + 1,
                width=p_width,
                height=p_height,
            )

            try:
                classification = classify_page_content(fitz_page, p_width, p_height)
                page_node.classification = classification

                page_links = extract_page_links(fitz_page)
                native_nodes: List[IRNode] = []
                table_nodes: List[IRNode] = []
                ocr_nodes: List[IRNode] = []
                image_nodes: List[IRNode] = []

                # Table extraction
                table_mask_rects: List[Rect] = []
                if is_table_candidate_page(fitz_page) and plumber_doc and page_idx < len(plumber_doc.pages):
                    extracted_tables = extract_table_nodes_from_pdfplumber(
                        plumber_doc.pages[page_idx], page_idx + 1
                    )
                    for t in extracted_tables:
                        table_nodes.append(t)
                        table_mask_rects.append(t.bbox)

                # Native text extraction (PyMuPDF)
                if classification in ("NATIVE", "MIXED", "UNCERTAIN"):
                    text_page = fitz_page.get_text("dict")
                    for block in text_page.get("blocks", []):
                        if block.get("type") == 0:  # Text block
                            b_rect = Rect(
                                x0=block["bbox"][0],
                                y0=block["bbox"][1],
                                x1=block["bbox"][2],
                                y1=block["bbox"][3],
                            )

                            if any(b_rect.intersects(tm) for tm in table_mask_rects):
                                continue

                            spans: List[TextSpan] = []
                            block_text_runs: List[str] = []
                            max_font_size = 10.0
                            has_bold = False

                            for line in block.get("lines", []):
                                for s in line.get("spans", []):
                                    stext = normalize_glyph_text(s.get("text", ""))
                                    if not stext:
                                        continue

                                    ssize = float(s.get("size", 10.0))
                                    sfont = s.get("font", "")
                                    sflags = int(s.get("flags", 0))

                                    s_rect = Rect(
                                        x0=s["bbox"][0],
                                        y0=s["bbox"][1],
                                        x1=s["bbox"][2],
                                        y1=s["bbox"][3],
                                    )
                                    s_link = associate_link_with_span(s_rect, page_links)

                                    bld = is_bold_font(sflags, sfont)
                                    it = is_italic_font(sflags, sfont)

                                    if bld:
                                        has_bold = True
                                    if ssize > max_font_size:
                                        max_font_size = ssize

                                    spans.append(
                                        TextSpan(
                                            text=stext,
                                            is_bold=bld,
                                            is_italic=it,
                                            font_size=ssize,
                                            font_name=sfont,
                                            link_url=s_link,
                                        )
                                    )
                                    block_text_runs.append(stext)

                            full_block_text = " ".join(block_text_runs).strip()
                            if not full_block_text:
                                continue

                            heading_lvl = classify_heading_level(
                                max_font_size,
                                base_font_size,
                                has_bold,
                                full_block_text,
                                is_isolated=True,
                            )

                            if heading_lvl:
                                node = HeadingNode(
                                    bbox=b_rect,
                                    page_number=page_idx + 1,
                                    source=BlockSource.PYMUPDF_NATIVE,
                                    level=heading_lvl,
                                    spans=spans,
                                )
                            elif BULLET_REGEX.match(full_block_text) or NUMBERED_LIST_REGEX.match(full_block_text):
                                is_num = bool(NUMBERED_LIST_REGEX.match(full_block_text))
                                list_item = ListItemNode(spans=spans, level=0)
                                node = ListNode(
                                    bbox=b_rect,
                                    page_number=page_idx + 1,
                                    source=BlockSource.PYMUPDF_NATIVE,
                                    ordered=is_num,
                                    items=[list_item],
                                )
                            else:
                                node = ParagraphNode(
                                    bbox=b_rect,
                                    page_number=page_idx + 1,
                                    source=BlockSource.PYMUPDF_NATIVE,
                                    spans=spans,
                                )

                            setattr(node, "raw_text", full_block_text)
                            node.role = classify_node_content_role(
                                node,
                                full_block_text,
                                p_height,
                                p_width,
                                repeated_headers,
                                repeated_footers,
                                font_size=max_font_size,
                                base_font_size=base_font_size,
                            )
                            native_nodes.append(node)

                # OCR pass for SCANNED or MIXED pages
                if classification in ("SCANNED", "MIXED"):
                    pix = fitz_page.get_pixmap(dpi=150)
                    # Accurate scaling factor from Pixmap pixel dimensions to PDF page points
                    scale_x = p_width / float(pix.width) if pix.width > 0 else 1.0
                    scale_y = p_height / float(pix.height) if pix.height > 0 else 1.0

                    ocr_lines = OCREngine.ocr_pixmap(pix, lang=lang)
                    pix = None

                    for oline in ocr_lines:
                        otext = oline["text"]
                        obbox = oline["bbox"]
                        o_rect = Rect(
                            x0=obbox[0] * scale_x,
                            y0=obbox[1] * scale_y,
                            x1=obbox[2] * scale_x,
                            y1=obbox[3] * scale_y,
                        )
                        if any(o_rect.intersects(tm) for tm in table_mask_rects):
                            continue

                        ocr_node = ParagraphNode(
                            bbox=o_rect,
                            page_number=page_idx + 1,
                            source=BlockSource.TESSERACT_OCR,
                            spans=[TextSpan(text=otext)],
                            confidence=oline["confidence"],
                        )
                        setattr(ocr_node, "raw_text", otext)
                        ocr_nodes.append(ocr_node)

                # Image extraction pass
                images_on_page = fitz_page.get_images()
                for img_idx, img_info in enumerate(images_on_page):
                    xref = img_info[0]
                    img_rects = fitz_page.get_image_rects(xref)
                    img_rect = Rect(x0=0, y0=0, x1=100, y1=100)
                    if img_rects:
                        img_rect = Rect(
                            x0=img_rects[0].x0,
                            y0=img_rects[0].y0,
                            x1=img_rects[0].x1,
                            y1=img_rects[0].y1,
                        )

                    image_key = None
                    if embed_images:
                        try:
                            base_img = doc.extract_image(xref)
                            if base_img:
                                b64 = base64.b64encode(base_img["image"]).decode("utf-8")
                                mime = f"image/{base_img['ext']}"
                                image_key = f"data:{mime};base64,{b64}"
                        except Exception as img_err:
                            logger.warning(f"Failed to extract image xref {xref}: {img_err}")

                    image_node = ImageNode(
                        bbox=img_rect,
                        page_number=page_idx + 1,
                        image_key=image_key,
                        alt_text=f"Figure {img_idx + 1}",
                    )
                    image_nodes.append(image_node)

                # Reconcile mixed nodes
                if classification == "MIXED":
                    combined_text_nodes = reconcile_mixed_page_nodes(native_nodes, ocr_nodes)
                elif classification == "SCANNED":
                    combined_text_nodes = ocr_nodes
                else:
                    combined_text_nodes = native_nodes

                # Assembly & spatial reading order layout sort with semantic grouping
                all_page_nodes = combined_text_nodes + table_nodes + image_nodes
                grouped_nodes = SemanticGrouper.process_page_nodes(all_page_nodes, p_width, p_height)
                page_node.nodes = grouped_nodes

            except Exception as page_exc:
                logger.error(f"Error processing page {page_idx + 1} of document '{input_path}': {page_exc}", exc_info=True)
                raise RuntimeError(f"Conversion failed on page {page_idx + 1}: {page_exc}") from page_exc

            ir_pages.append(page_node)

    finally:
        if plumber_doc:
            try:
                plumber_doc.close()
            except Exception:
                pass
        doc.close()

    if progress_cb:
        progress_cb(90, "Compiling Markdown document")

    doc_ir = DocumentIR(
        filename=input_path,
        total_pages=total_pages,
        base_font_size=base_font_size,
        pages=ir_pages,
    )

    markdown_output = MarkdownRenderer.render(
        doc_ir, include_annotations=include_annotations
    )

    if progress_cb:
        progress_cb(100, "Markdown conversion complete")

    return markdown_output

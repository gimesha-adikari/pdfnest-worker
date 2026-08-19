import base64
import os
import tempfile
import pytest
import fitz

from app.api.tools.markdown.classifier import (
    classify_node_content_role,
    classify_page_content,
    is_multi_signal_watermark,
    reconcile_mixed_page_nodes,
)
from app.api.tools.markdown.extractor import (
    calculate_document_base_font_size,
    classify_heading_level,
    normalize_glyph_text,
)
from app.api.tools.markdown.ir import (
    BlockSource,
    CodeBlockNode,
    ContentRole,
    DocumentIR,
    HeadingNode,
    ImageNode,
    ListItemNode,
    ListNode,
    PageNode,
    ParagraphNode,
    Rect,
    TableCellNode,
    TableNode,
    TextSpan,
)
from app.api.tools.markdown.layout import (
    LayoutRegion,
    partition_page_into_layout_regions,
    sort_nodes_in_topological_reading_order,
)
from app.api.tools.markdown.renderer import MarkdownRenderer, escape_markdown_prose
from app.api.tools.markdown.service import convert_pdf_to_markdown
from app.core.ocr_engine import get_tessdata_dir, validate_ocr_language


def create_sample_pdf(pages_data) -> str:
    """Helper to create temporary PDF documents with specified text lines and positions."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = tmp.name
    tmp.close()

    doc = fitz.open()
    for page_lines in pages_data:
        page = doc.new_page(width=600, height=800)
        for line in page_lines:
            text = line.get("text", "")
            point = fitz.Point(line.get("x", 50), line.get("y", 50))
            size = line.get("size", 11)
            fontname = line.get("fontname", "helv")
            page.insert_text(point, text, fontsize=size, fontname=fontname)
    doc.save(tmp_path)
    doc.close()
    return tmp_path


def create_pdf_with_image() -> str:
    """Helper to create a PDF containing an embedded image draw stream."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = tmp.name
    tmp.close()

    doc_img = fitz.open()
    p_img = doc_img.new_page(width=100, height=100)
    p_img.draw_rect((0, 0, 100, 100), fill=(0, 0.5, 1))
    pix = p_img.get_pixmap()
    doc_img.close()

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text(fitz.Point(50, 50), "Document Image Test Header", fontsize=16)

    img_rect = fitz.Rect(50, 100, 250, 300)
    page.insert_image(img_rect, pixmap=pix)

    doc.save(tmp_path)
    doc.close()
    return tmp_path


# --- 1. Basic Paragraphs & Headings ---

def test_md_01_e2e_simple_paragraphs():
    pdf_path = create_sample_pdf([
        [{"text": "Simple paragraph line 1.", "x": 50, "y": 100}]
    ])
    try:
        md = convert_pdf_to_markdown(pdf_path)
        assert "Simple paragraph line 1." in md
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


def test_md_02_e2e_heading_hierarchy():
    pdf_path = create_sample_pdf([
        [
            {"text": "Document Title", "x": 50, "y": 80, "size": 24},
            {"text": "Section 1 Overview", "x": 50, "y": 140, "size": 18},
            {"text": "Body paragraph text.", "x": 50, "y": 180, "size": 11},
        ]
    ])
    try:
        md = convert_pdf_to_markdown(pdf_path)
        assert "# Document Title" in md
        assert "## Section 1 Overview" in md
        assert "Body paragraph text." in md
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


# --- 2. Lists & Bullet Glyphs ---

def test_md_03_bullet_glyphs_and_lists():
    normalized = normalize_glyph_text("(cid:127) Bullet point text")
    assert normalized == "• Bullet point text"

    doc_ir = DocumentIR(
        filename="test.pdf",
        total_pages=1,
        pages=[
            PageNode(
                page_number=1,
                width=600,
                height=800,
                nodes=[
                    ListNode(
                        bbox=Rect(50, 50, 550, 100),
                        page_number=1,
                        ordered=False,
                        items=[
                            ListItemNode(spans=[TextSpan(text="First bullet item")], level=0),
                            ListItemNode(spans=[TextSpan(text="Second bullet item")], level=0),
                        ],
                    )
                ],
            )
        ],
    )
    md = MarkdownRenderer.render(doc_ir)
    assert "- First bullet item" in md
    assert "- Second bullet item" in md


# --- 3. GFM Tables ---

def test_md_04_gfm_table_rendering():
    doc_ir = DocumentIR(
        filename="test.pdf",
        total_pages=1,
        pages=[
            PageNode(
                page_number=1,
                width=600,
                height=800,
                nodes=[
                    TableNode(
                        bbox=Rect(50, 50, 550, 200),
                        page_number=1,
                        headers=[TableCellNode(text="Col A"), TableCellNode(text="Col B")],
                        rows=[
                            [TableCellNode(text="Val A1"), TableCellNode(text="Val B1")],
                            [TableCellNode(text="Val A2"), TableCellNode(text="Val B2")],
                        ],
                        alignments=["left", "left"],
                        upstream_origin="MarkItDown adapted",
                    )
                ],
            )
        ],
    )
    md = MarkdownRenderer.render(doc_ir)
    assert "| Col A | Col B |" in md
    assert "| :--- | :--- |" in md
    assert "| Val A1 | Val B1 |" in md


# --- 4. Layout & Region Segmentation ---

def test_md_06_two_column_topological_reading_order():
    node_left = ParagraphNode(
        bbox=Rect(50, 100, 250, 150),
        page_number=1,
        spans=[TextSpan(text="Left column text")],
    )
    node_right = ParagraphNode(
        bbox=Rect(350, 100, 550, 150),
        page_number=1,
        spans=[TextSpan(text="Right column text")],
    )
    sorted_nodes = sort_nodes_in_topological_reading_order(
        [node_right, node_left], page_width=600, page_height=800
    )
    assert sorted_nodes[0].spans[0].text == "Left column text"
    assert sorted_nodes[1].spans[0].text == "Right column text"


def test_md_07_changing_layout_regions_on_same_page():
    h1 = HeadingNode(
        bbox=Rect(50, 40, 550, 80),
        page_number=1,
        level=1,
        spans=[TextSpan(text="Main Banner Title")],
    )
    col_left = ParagraphNode(
        bbox=Rect(50, 120, 250, 200),
        page_number=1,
        spans=[TextSpan(text="Left Column Content")],
    )
    col_right = ParagraphNode(
        bbox=Rect(350, 120, 550, 200),
        page_number=1,
        spans=[TextSpan(text="Right Column Content")],
    )
    table = TableNode(
        bbox=Rect(50, 250, 550, 400),
        page_number=1,
        headers=[TableCellNode(text="Header 1"), TableCellNode(text="Header 2")],
        rows=[[TableCellNode(text="Data 1"), TableCellNode(text="Data 2")]],
    )
    footer = ParagraphNode(
        bbox=Rect(50, 750, 550, 780),
        page_number=1,
        role=ContentRole.PRIMARY,
        spans=[TextSpan(text="Bottom Banner Text")],
    )

    regions = partition_page_into_layout_regions([h1, col_left, col_right, table, footer], 600, 800)
    assert len(regions) >= 3

    sorted_nodes = sort_nodes_in_topological_reading_order(
        [table, col_right, h1, col_left, footer], page_width=600, page_height=800
    )

    assert sorted_nodes[0] == h1
    assert sorted_nodes[1] == col_left
    assert sorted_nodes[2] == col_right


# --- 5. True E2E Image Embedding ---

def test_md_09_true_e2e_image_embedding():
    pdf_path = create_pdf_with_image()
    try:
        # 1. embed_images = True -> Output contains base64 Data-URI
        md_embed = convert_pdf_to_markdown(pdf_path, embed_images=True)
        assert "![Figure 1](data:image/png;base64," in md_embed

        # 2. embed_images = False -> Output contains clean baseline figure placeholder
        md_baseline = convert_pdf_to_markdown(pdf_path, embed_images=False)
        assert "**Figure 1**" in md_baseline
        assert "data:image" not in md_baseline
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


# --- 6. Multi-Signal Watermark Classification ---

def test_md_12_multi_signal_watermark_classification():
    dummy_node = ParagraphNode(bbox=Rect(200, 300, 400, 500), page_number=1)

    # 1. Centered, semi-transparent, large font "CONFIDENTIAL" watermark -> WATERMARK
    is_wm = is_multi_signal_watermark(
        dummy_node,
        text="CONFIDENTIAL",
        page_height=800,
        page_width=600,
        is_rotated=True,
        font_size=28.0,
        base_font_size=10.0,
        opacity=0.40,
    )
    assert is_wm is True

    # 2. Legitimate rotated text without watermark keywords/opacity -> NOT WATERMARK (PRIMARY)
    legit_rotated = is_multi_signal_watermark(
        ParagraphNode(bbox=Rect(50, 100, 100, 400), page_number=1),
        text="Vertical Table Header Text",
        page_height=800,
        page_width=600,
        is_rotated=True,
        font_size=10.0,
        base_font_size=10.0,
        opacity=1.0,
    )
    assert legit_rotated is False

    # 3. Normal paragraph containing word "DRAFT" in body flow -> NOT WATERMARK (PRIMARY)
    body_draft = is_multi_signal_watermark(
        ParagraphNode(bbox=Rect(50, 600, 500, 620), page_number=1),
        text="This is a draft version of the report document.",
        page_height=800,
        page_width=600,
        is_rotated=False,
        font_size=10.0,
        base_font_size=10.0,
        opacity=1.0,
    )
    assert body_draft is False


# --- 7. Mixed-Page Role-Aware Reconciliation ---

def test_md_14_mixed_page_role_aware_reconciliation():
    # 1. Primary Native + Equivalent OCR -> Suppress OCR
    n_primary = ParagraphNode(
        bbox=Rect(50, 100, 500, 150),
        page_number=1,
        role=ContentRole.PRIMARY,
        source=BlockSource.PYMUPDF_NATIVE,
    )
    setattr(n_primary, "raw_text", "Exact Native Paragraph Text")

    ocr_equiv = ParagraphNode(
        bbox=Rect(52, 102, 498, 148),
        page_number=1,
        role=ContentRole.PRIMARY,
        source=BlockSource.TESSERACT_OCR,
    )
    setattr(ocr_equiv, "raw_text", "Exact Native Paragraph Text")

    rec1 = reconcile_mixed_page_nodes([n_primary], [ocr_equiv])
    assert len(rec1) == 1
    assert rec1[0] == n_primary

    # 2. Native WATERMARK + OCR Body -> Keep OCR
    n_wm = ParagraphNode(
        bbox=Rect(100, 200, 500, 600),
        page_number=1,
        role=ContentRole.WATERMARK,
        source=BlockSource.PYMUPDF_NATIVE,
    )
    setattr(n_wm, "raw_text", "WATERMARK DRAFT")

    ocr_body = ParagraphNode(
        bbox=Rect(150, 300, 450, 350),
        page_number=1,
        role=ContentRole.PRIMARY,
        source=BlockSource.TESSERACT_OCR,
    )
    setattr(ocr_body, "raw_text", "Scanned Document Body Paragraph")

    rec2 = reconcile_mixed_page_nodes([n_wm], [ocr_body])
    assert len(rec2) == 2  # OCR was NOT suppressed by watermark!

    # 3. Native HEADER + OCR Body -> Keep OCR
    n_hdr = ParagraphNode(
        bbox=Rect(50, 10, 550, 30),
        page_number=1,
        role=ContentRole.HEADER,
        source=BlockSource.PYMUPDF_NATIVE,
    )
    setattr(n_hdr, "raw_text", "Page Header Text")

    rec3 = reconcile_mixed_page_nodes([n_hdr], [ocr_body])
    assert len(rec3) == 2


# --- 8. Header / Footer Repetition Filtering ---

def test_md_16_header_footer_content_role_filtering():
    doc_ir = DocumentIR(
        filename="test.pdf",
        total_pages=1,
        pages=[
            PageNode(
                page_number=1,
                width=600,
                height=800,
                nodes=[
                    ParagraphNode(
                        bbox=Rect(50, 10, 550, 30),
                        page_number=1,
                        role=ContentRole.HEADER,
                        spans=[TextSpan(text="Running Header")],
                    ),
                    ParagraphNode(
                        bbox=Rect(50, 100, 550, 150),
                        page_number=1,
                        role=ContentRole.PRIMARY,
                        spans=[TextSpan(text="Main Body Content")],
                    ),
                    ParagraphNode(
                        bbox=Rect(50, 780, 550, 795),
                        page_number=1,
                        role=ContentRole.FOOTER,
                        spans=[TextSpan(text="Page 1 of 10")],
                    ),
                ],
            )
        ],
    )
    md = MarkdownRenderer.render(doc_ir)
    assert "Main Body Content" in md
    assert "Running Header" not in md
    assert "Page 1 of 10" not in md


# --- 9. Encrypted PDF Authentication ---

def test_md_18_encrypted_pdf_password_validation():
    pdf_path = create_sample_pdf([[{"text": "Secret content", "x": 50, "y": 50}]])
    doc = fitz.open(pdf_path)
    enc_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    doc.save(enc_path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="correct_password", owner_pw="owner_password")
    doc.close()
    os.remove(pdf_path)

    try:
        with pytest.raises(fitz.FileDataError):
            convert_pdf_to_markdown(enc_path, password="wrong_password")

        md = convert_pdf_to_markdown(enc_path, password="correct_password")
        assert "Secret content" in md
    finally:
        if os.path.exists(enc_path):
            os.remove(enc_path)


# --- 10. OCR Language Validation ---

def test_md_20_ocr_language_validation():
    # 1. Valid language -> eng
    valid = validate_ocr_language("eng")
    assert valid == "eng"

    # 2. Missing language -> raises ValueError
    with pytest.raises(ValueError, match="not installed on worker"):
        validate_ocr_language("nonexistent_lang_xyz")


# --- 11. Multilingual Unicode ---

def test_md_21_multilingual_unicode():
    pdf_path = create_sample_pdf([
        [
            {"text": "English section", "x": 50, "y": 50},
            {"text": "中文 : PDF 转 Markdown 测试", "x": 50, "y": 100, "fontname": "china-s"},
        ]
    ])
    try:
        md = convert_pdf_to_markdown(pdf_path)
        assert "English section" in md
        assert "中文 : PDF 转 Markdown 测试" in md
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


# --- 12. Markdown Escaping ---

def test_md_markdown_escaping():
    escaped = escape_markdown_prose("User *bold* _italic_ #tag [link]")
    assert "\\*" in escaped
    assert "\\_" in escaped
    assert "\\#" in escaped
    assert "\\[" in escaped


def test_cv_semantic_structure_and_grouping():
    cv_pdf_path = "/home/gimesha/Downloads/Gimesha Nirmal – Software Engineer CV.pdf"
    if os.path.exists(cv_pdf_path):
        md = convert_pdf_to_markdown(cv_pdf_path)

        # 1. Header precedence: Name & Title before Summary
        name_idx = md.find("GIMESHA")
        title_idx = md.find("SOFTWARE ENGINEER")
        summary_idx = md.find("SUMMARY")

        assert name_idx != -1 and title_idx != -1 and summary_idx != -1
        assert name_idx < title_idx < summary_idx

        # 2. Section presence
        assert "EDUCATION" in md
        assert "SKILLS" in md
        assert "LANGUAGES" in md
        assert "PROJECTS" in md
        assert "LEADERSHIP" in md

        # 3. Column isolation: Left column sections should not interleave right column projects
        projects_idx = md.find("PROJECTS")
        skills_idx = md.find("SKILLS")
        pdfnest_idx = md.find("PDFNest")

        assert projects_idx < pdfnest_idx


from __future__ import annotations

import io

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_v2.contracts import OCRLine, OCRToken, PageContentClassification, PageGeometry, PageProcessingSource, PageResult, PageStatus, Rect
from app.core.ocr_v2.structured import StructuredDocumentProcessor, StructuredElementType, _ocr_structured_elements, render_structured_markdown


def _pdf_with_scanned_text(path) -> None:
    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    draw.text((50, 80), "Scanned paragraph 123", fill="black", font=font)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    document = fitz.open()
    page = document.new_page(width=612, height=255)
    page.insert_image(page.rect, stream=encoded.getvalue())
    document.save(path)
    document.close()


def test_native_document_uses_native_structured_elements():
    result = StructuredDocumentProcessor().process_document("/home/gimesha/My_Projects/platen/pdfnest/tests/fixtures/normal_text.pdf")

    assert result.schema_version == "ocr_v2_structured_document.v1"
    assert result.validation["valid"] is True
    assert all(page.processing_source == "NATIVE_EXTRACTION" for page in result.pages)
    assert any(element.type is StructuredElementType.HEADING for page in result.pages for element in page.elements)
    assert "WORD_GEOMETRY" in result.capabilities
    assert "FORMULA_STRUCTURE_UNAVAILABLE_WITH_CURRENT_LOCAL_ENGINES" not in result.warnings


def test_scanned_document_uses_real_tesseract_structured_blocks(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    _pdf_with_scanned_text(str(pdf_path))

    result = StructuredDocumentProcessor().process_document(str(pdf_path))

    assert result.validation["valid"] is True
    assert result.pages[0].classification == "IMAGE_SCAN"
    assert result.pages[0].processing_source == "OCR_RECOGNITION"
    assert any(element.type is StructuredElementType.PARAGRAPH for element in result.pages[0].elements)
    assert "WORD_GEOMETRY" in result.capabilities
    assert "FORMULA_STRUCTURE_UNAVAILABLE_WITH_CURRENT_LOCAL_ENGINES" in result.warnings
    assert "Scanned" in render_structured_markdown(result)


def test_structured_markdown_does_not_invent_unsupported_formula_or_table():
    result = StructuredDocumentProcessor().process_document("/home/gimesha/My_Projects/platen/pdfnest/tests/fixtures/normal_text.pdf")
    markdown = render_structured_markdown(result)

    assert "$$" not in markdown
    assert "| :---" not in markdown


def _synthetic_page(lines: list[tuple[str, float, list[tuple[str, float]]]]) -> PageResult:
    ocr_lines = []
    tokens = []
    for index, (text, y, token_values) in enumerate(lines):
        token_ids = []
        for token_index, (token_text, x) in enumerate(token_values):
            token_id = f"t-{index}-{token_index}"
            token_ids.append(token_id)
            tokens.append(OCRToken(token_id, token_text, Rect(x, y, max(5.0, len(token_text) * 4.0), 8.0)))
        ocr_lines.append(OCRLine(f"line-{index}", text, Rect(40.0, y, 420.0, 8.0), tuple(token_ids)))
    return PageResult(
        page_index=0,
        page_id="page-0",
        geometry=PageGeometry(500.0, 500.0),
        content_classification=PageContentClassification.IMAGE_SCAN,
        processing_source=PageProcessingSource.OCR_RECOGNITION,
        status=PageStatus.SUCCESS,
        text="\n".join(item[0] for item in lines),
        tokens=tuple(tokens),
        lines=tuple(ocr_lines),
    )


def test_scanned_geometry_recovers_headings_and_grouped_paragraphs():
    page = _synthetic_page([
        ("FACULTY OF ENGINEERING TECHNOLOGY", 40.0, [("FACULTY", 170.0), ("OF", 220.0), ("ENGINEERING", 240.0), ("TECHNOLOGY", 300.0)]),
        ("This is the first body line", 75.0, [("This", 40.0), ("is", 70.0), ("the", 85.0), ("first", 105.0), ("body", 135.0), ("line", 165.0)]),
        ("This is the second body line", 84.0, [("This", 40.0), ("is", 70.0), ("the", 85.0), ("second", 105.0), ("body", 140.0), ("line", 170.0)]),
        ("Confirmation of Academic Details", 115.0, [("Confirmation", 175.0), ("of", 230.0), ("Academic", 245.0), ("Details", 290.0)]),
    ])
    elements = _ocr_structured_elements(page)
    assert [element.type for element in elements].count(StructuredElementType.HEADING) == 2
    assert any(element.type is StructuredElementType.PARAGRAPH and "first body line" in element.text and "second body line" in element.text for element in elements)


def test_scanned_geometry_recovers_only_repeated_aligned_simple_table():
    rows = [("01 Module Alpha 21 Followed", 60.0), ("02 Module Beta 16 Followed", 72.0), ("03 Module Gamma 14 Followed", 84.0), ("04 Module Delta 10 Followed", 96.0)]
    lines = [("NO MODULE CREDITS STATUS", 48.0, [("NO", 40.0), ("MODULE", 120.0), ("CREDITS", 300.0), ("STATUS", 360.0)])]
    for text, y in rows:
        parts = text.split()
        lines.append((text, y, [(parts[0], 40.0), (f"{parts[1]} {parts[2]}", 120.0), (parts[3], 300.0), (parts[4], 360.0)]))
    elements = _ocr_structured_elements(_synthetic_page(lines))
    table = next(element for element in elements if element.type is StructuredElementType.TABLE)
    assert table.data["row_count"] == 4
    assert "| NO | MODULE | CREDITS | STATUS |" in render_structured_markdown(type("Result", (), {"pages": [type("Page", (), {"elements": tuple(elements), "reading_order": tuple(element.element_id for element in elements)})()]})())


def test_scanned_geometry_does_not_fabricate_ambiguous_table():
    page = _synthetic_page([
        ("01 One 21", 60.0, [("01", 40.0), ("One", 120.0), ("21", 300.0)]),
        ("02 Two 16", 72.0, [("02", 40.0), ("Two", 120.0), ("16", 300.0)]),
        ("03 Three 14", 84.0, [("03", 40.0), ("Three", 120.0), ("14", 300.0)]),
    ])
    assert not any(element.type is StructuredElementType.TABLE for element in _ocr_structured_elements(page))


def test_scanned_geometry_recovers_obvious_lists_but_not_numbered_prose():
    list_page = _synthetic_page([
        ("1. First requirement", 60.0, [("1.", 40.0), ("First", 60.0), ("requirement", 95.0)]),
        ("2. Second requirement", 72.0, [("2.", 40.0), ("Second", 60.0), ("requirement", 100.0)]),
    ])
    list_elements = _ocr_structured_elements(list_page)
    assert any(element.type is StructuredElementType.LIST for element in list_elements)

    prose_page = _synthetic_page([
        ("The result is 1.5 percent.", 60.0, [("The", 40.0), ("result", 65.0), ("is", 105.0), ("1.5", 120.0), ("percent.", 145.0)]),
    ])
    assert not any(element.type is StructuredElementType.LIST for element in _ocr_structured_elements(prose_page))


def test_mixed_document_keeps_native_and_scanned_page_paths(tmp_path):
    scanned_pdf = tmp_path / "scanned.pdf"
    _pdf_with_scanned_text(str(scanned_pdf))
    mixed_pdf = tmp_path / "mixed.pdf"
    with fitz.open("/home/gimesha/My_Projects/platen/pdfnest/tests/fixtures/normal_text.pdf") as native_document, fitz.open(str(scanned_pdf)) as scanned_document, fitz.open() as mixed_document:
        mixed_document.insert_pdf(native_document, from_page=0, to_page=0)
        mixed_document.insert_pdf(scanned_document, from_page=0, to_page=0)
        mixed_document.save(str(mixed_pdf))

    result = StructuredDocumentProcessor().process_document(str(mixed_pdf))

    assert result.validation["valid"] is True
    assert result.pages[0].processing_source == "NATIVE_EXTRACTION"
    assert result.pages[1].processing_source == "OCR_RECOGNITION"
    assert any(element.type is StructuredElementType.HEADING for element in result.pages[0].elements)
    assert any(element.type is StructuredElementType.PARAGRAPH for element in result.pages[1].elements)
    assert len({element.element_id for page in result.pages for element in page.elements}) == sum(len(page.elements) for page in result.pages)

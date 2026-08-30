from __future__ import annotations

import io

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_v2.structured import StructuredDocumentProcessor, StructuredElementType, render_structured_markdown


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
    assert any(element.type is StructuredElementType.TEXT_BLOCK for element in result.pages[0].elements)
    assert "WORD_GEOMETRY" in result.capabilities
    assert "FORMULA_STRUCTURE_UNAVAILABLE_WITH_CURRENT_LOCAL_ENGINES" in result.warnings
    assert "Scanned" in render_structured_markdown(result)


def test_structured_markdown_does_not_invent_unsupported_formula_or_table():
    result = StructuredDocumentProcessor().process_document("/home/gimesha/My_Projects/platen/pdfnest/tests/fixtures/normal_text.pdf")
    markdown = render_structured_markdown(result)

    assert "$$" not in markdown
    assert "| :---" not in markdown

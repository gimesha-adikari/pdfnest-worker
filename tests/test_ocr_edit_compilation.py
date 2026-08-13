from __future__ import annotations

import json
import tempfile
import fitz
from app.api.tools.editor.document import compile_document, extract_document

def create_sample_ocr_pdf() -> str:
    """Create a sample scanned-style single-page PDF with raster image text."""
    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    
    # Create an image containing text to simulate a scanned PDF
    pix = fitz.Pixmap(fitz.csRGB, fitz.Rect(0, 0, 600, 800), False)
    pix.clear_with(255)
    page.insert_image(page.rect, pixmap=pix)
    
    doc.save(temp_pdf.name)
    doc.close()
    return temp_pdf.name


def test_compile_document_ocr_scanned_page_text_replacement():
    """Verify that scanned/OCR PDF compilation replaces background image area AND renders replacement text."""
    sample_pdf_path = create_sample_ocr_pdf()
    output_pdf_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    pages_json_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "width": 600,
                "height": 800,
                "kind": "scanned",
                "is_ocr": True,
                "elements": [
                    {
                        "text": "ORIGINAL OCR TEXT",
                        "original_text": "ORIGINAL OCR TEXT",
                        "x": 50,
                        "y": 100,
                        "width": 200,
                        "height": 20,
                        "size": 14,
                        "font": "helv",
                    },
                    {
                        "text": "REPLACEMENT OCR TEXT SUCCESS",
                        "original_text": "OLD UNEDITED TEXT",
                        "x": 50,
                        "y": 200,
                        "width": 250,
                        "height": 20,
                        "size": 14,
                        "font": "helv",
                        "text_color": "#16a34a",
                    },
                ],
            }
        ]
    }

    with open(pages_json_path, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    # Execute compilation
    compile_document(sample_pdf_path, output_pdf_path, pages_json_path)

    # Inspect compiled PDF output
    with fitz.open(output_pdf_path) as out_doc:
        assert len(out_doc) == 1
        out_page = out_doc[0]
        out_text = out_page.get_text()

        # The replacement text MUST be rendered onto the compiled PDF page!
        assert "REPLACEMENT OCR TEXT SUCCESS" in out_text
        assert "ORIGINAL OCR TEXT" not in out_text

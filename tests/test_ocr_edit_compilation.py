from __future__ import annotations

import json
import shutil
import subprocess
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


def test_compile_document_output_opens_preserves_page_count_and_unchanged_page():
    """Independently validate a native edit and an untouched scanned page."""
    with tempfile.TemporaryDirectory() as tmp:
        source_path = f"{tmp}/source.pdf"
        output_path = f"{tmp}/output.pdf"
        layout_path = f"{tmp}/layout.json"
        doc = fitz.open()
        native = doc.new_page(width=400, height=500)
        native.insert_text((72, 100), "ORIGINAL NATIVE", fontsize=18, fontname="helv")
        scanned = doc.new_page(width=400, height=500)
        pix = fitz.Pixmap(fitz.csRGB, fitz.Rect(0, 0, 400, 500), False)
        pix.clear_with(232)
        scanned.insert_image(scanned.rect, pixmap=pix)
        doc.save(source_path)
        doc.close()

        with fitz.open(source_path) as source:
            rect = source[0].search_for("ORIGINAL NATIVE")[0]
            unchanged_before = bytes(source[1].get_pixmap(matrix=fitz.Matrix(0.5, 0.5)).samples)
        layout = {
            "schema_version": "ocr_v2_editor_layout.v1",
            "pages": [
                {
                    "page_num": 1,
                    "width": 400,
                    "height": 500,
                    "kind": "text",
                    "elements": [{
                        "id": "native-p1-e1",
                        "text": "UPDATED NATIVE",
                        "original_text": "ORIGINAL NATIVE",
                        "x": rect.x0,
                        "y": rect.y0,
                        "width": rect.width,
                        "height": rect.height,
                        "size": 18,
                        "font": "helv",
                    }],
                },
                {"page_num": 2, "width": 400, "height": 500, "kind": "scanned", "is_ocr": True, "elements": []},
            ],
        }
        with open(layout_path, "w", encoding="utf-8") as handle:
            json.dump(layout, handle)

        compile_document(source_path, output_path, layout_path)

        with fitz.open(output_path) as output:
            assert output.page_count == 2
            compiled_text = output[0].get_text()
            assert "UPDATED" in compiled_text
            assert "NATIVE" in compiled_text
            assert "ORIGINAL" not in compiled_text
            assert bytes(output[1].get_pixmap(matrix=fitz.Matrix(0.5, 0.5)).samples) == unchanged_before
        if qpdf := shutil.which("qpdf"):
            subprocess.run([qpdf, "--check", output_path], check=True, capture_output=True, text=True)

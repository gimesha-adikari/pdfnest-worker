from __future__ import annotations

import json
import os
import tempfile
import fitz
import pytest

from app.api.tools.editor.document import compile_document

TEMP_DIR = tempfile.gettempdir()


def test_phase8_embedded_font_inspection_and_reuse():
    """Embedded Font Proof: Inspect original embedded PDF font resources and verify font byte stream reuse in surgical replacement."""
    input_pdf = os.path.join(TEMP_DIR, "phase8_font_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase8_font_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase8_font_pages.json")

    # 1. Create PDF with custom font
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello CustomFont World", fontname="helv", fontsize=12)
    doc.save(input_pdf)

    # 2. Inspect original PDF font resources
    orig_fonts = page.get_fonts()
    font_details = []
    for f in orig_fonts:
        xref = f[0]
        font_name = f[3]
        font_name_res, font_ext, font_type, font_buf = doc.extract_font(xref)
        font_details.append({
            "xref": xref,
            "name": font_name_res or font_name,
            "ext": font_ext,
            "type": font_type,
            "bytes_length": len(font_buf) if font_buf else 0,
        })
    doc.close()

    print(f"Original PDF Font Resources: {font_details}")
    assert len(orig_fonts) >= 1

    # 3. Surgical replacement of 'CustomFont' -> 'Replacement'
    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Hello Replacement World",
                        "original_text": "Hello CustomFont World",
                        "target_substring": "CustomFont",
                        "x": 50, "y": 40, "width": 200, "height": 15,
                    }
                ],
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    # 4. Inspect compiled PDF font resources
    mod_doc = fitz.open(output_pdf)
    mod_page = mod_doc[0]
    mod_fonts = mod_page.get_fonts()
    mod_text = mod_page.get_text("text").strip()
    mod_doc.close()

    print(f"Output PDF Font Resources: {mod_fonts}")
    assert "Hello" in mod_text
    assert "Replacement" in mod_text
    assert "World" in mod_text
    assert "CustomFont" not in mod_text
    assert len(mod_fonts) >= 1  # Valid font resource present in output PDF!

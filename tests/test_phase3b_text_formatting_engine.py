from __future__ import annotations

import os
import tempfile
import fitz
import pytest

from app.api.tools.editor.document import (
    is_element_dirty,
    resolve_surgical_targets,
    render_surgical_replacement,
    compile_document,
)

TEMP_DIR = tempfile.gettempdir()


def test_phase3b_rich_text_formatting_matrix():
    """Test full matrix of text formatting operations: bold, italic, color, underline, strikethrough, user bg."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # Background rect (Type A)
    page.draw_rect(fitz.Rect(30, 30, 300, 80), color=None, fill=(0.85, 0.85, 0.85))
    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50,
        "y": 40,
        "width": 200,
        "height": 15,
        "size": 12,
        "style": {
            "fontFamily": "helv",
            "fontSize": 14,
            "bold": True,
            "italic": True,
            "color": "#FF0000",
            "underline": True,
            "strikethrough": True,
            "background": {"enabled": True, "color": "#FFFF00"},
        },
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1

    metrics = render_surgical_replacement(page, targets[0], element=element, fill_color=None)
    assert metrics["style"]["bold"] is True
    assert metrics["style"]["italic"] is True
    assert metrics["style"]["underline"] is True
    assert metrics["style"]["strikethrough"] is True
    assert metrics["style"]["user_bg_enabled"] is True
    assert metrics["style"]["user_bg_color"] == "#FFFF00"
    assert metrics["style"]["font_code"] == "hebi"

    out_pdf = os.path.join(TEMP_DIR, "phase3b_rich_formatting.pdf")
    doc.save(out_pdf)
    doc.close()

    # Re-open and verify text and vector drawing layers
    mod_doc = fitz.open(output_pdf := out_pdf)
    text_extracted = mod_doc[0].get_text("text").strip()
    drawings = mod_doc[0].get_drawings()
    mod_doc.close()

    assert "wonderful" in text_extracted
    assert len(drawings) >= 3  # Original gray bg + Type B yellow highlight + underline/strikethrough lines!


def test_phase3b_compile_document_skips_unchanged_elements():
    """Verify production compile_document() leaves unchanged lines 100% untouched."""
    import json
    input_pdf = os.path.join(TEMP_DIR, "phase3b_compile_input.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase3b_compile_output.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase3b_compile_pages.json")

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Line 1 Unchanged", fontname="helv", fontsize=12)
    page.insert_text((50, 100), "Line 2 Original", fontname="helv", fontsize=12)
    doc.save(input_pdf)
    doc.close()

    # Edit only Line 2
    document_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Line 1 Unchanged",
                        "original_text": "Line 1 Unchanged",
                        "x": 50, "y": 40, "width": 100, "height": 15,
                    },
                    {
                        "text": "Line 2 Modified",
                        "original_text": "Line 2 Original",
                        "x": 50, "y": 90, "width": 100, "height": 15,
                    },
                ],
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(document_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    mod_doc = fitz.open(output_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Line 1 Unchanged" in text_out
    assert "Line 2 Modified" in text_out
    assert "Line 2 Original" not in text_out


def test_phase3b_type_a_original_background_preservation():
    """Verify Type A original background preservation: gray rectangle remains gray."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.draw_rect(fitz.Rect(30, 30, 300, 80), color=None, fill=(0.7, 0.7, 0.7)) # Gray box
    page.insert_text((50, 50), "Original Text Here", fontname="helv", fontsize=12)

    element = {
        "text": "Edited Text Here",
        "original_text": "Original Text Here",
        "x": 50, "y": 40, "width": 150, "height": 15,
    }

    targets = resolve_surgical_targets(page, element)
    render_surgical_replacement(page, targets[0], element=element, fill_color=None)

    out_pdf = os.path.join(TEMP_DIR, "phase3b_type_a_bg.pdf")
    doc.save(out_pdf)
    doc.close()

    mod_doc = fitz.open(out_pdf)
    drawings = mod_doc[0].get_drawings()
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Edited" in text_out
    assert "Text Here" in text_out
    assert "Original" not in text_out
    assert len(drawings) > 0
    assert drawings[0]["fill"] == pytest.approx((0.7, 0.7, 0.7), abs=0.01)  # Original gray fill preserved!

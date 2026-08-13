from __future__ import annotations

import json
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


def test_phase3c_style_only_edit():
    """Verify style-only edits (text unchanged, style override dirty) identify target line and render override."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Style Only Text", fontname="helv", fontsize=12)

    element = {
        "text": "Style Only Text",
        "original_text": "Style Only Text",
        "target_substring": "Style Only Text",
        "x": 50,
        "y": 40,
        "width": 150,
        "height": 15,
        "style": {"bold": True, "color": "#FF0000", "underline": True},
    }

    assert is_element_dirty(element) is True

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1
    assert targets[0]["original_substring"] == "Style Only Text"

    metrics = render_surgical_replacement(page, targets[0], element=element)
    assert metrics["style"]["bold"] is True
    assert metrics["style"]["color"] == "#FF0000"
    assert metrics["style"]["underline"] is True
    doc.close()


def test_phase3c_neighbor_collision_detection():
    """Verify neighbor collision detection correctly measures available space and flags collisions."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello beautiful", fontname="helv", fontsize=12)

    # 1. Fits within available whitespace at end of line ('beautiful' -> 'wonderful')
    element_fit = {
        "text": "Hello wonderful",
        "original_text": "Hello beautiful",
        "x": 50, "y": 40, "width": 150, "height": 15,
    }
    targets_fit = resolve_surgical_targets(page, element_fit)
    metrics_fit = render_surgical_replacement(page, targets_fit[0], element=element_fit)
    assert metrics_fit["collides"] is False
    assert metrics_fit["font_scaled"] is False

    # 2. Collision case where text would overlap neighboring word
    page2 = doc.new_page(width=600, height=800)
    page2.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12)
    element_collide = {
        "text": "Hello extraordinarily-gorgeous-wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15,
    }
    targets_col = resolve_surgical_targets(page2, element_collide)
    metrics_col = render_surgical_replacement(page2, targets_col[0], element=element_collide)
    assert metrics_col["collides"] is True
    assert len(metrics_col["warnings"]) > 0
    doc.close()


def test_phase3c_real_pdf_multi_line_regression():
    """Real PDF fidelity test: editing 1 word on line 2 leaves lines 1 and 3 100% untouched."""
    input_pdf = os.path.join(TEMP_DIR, "phase3c_regression_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase3c_regression_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase3c_regression_pages.json")

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Line 1 Top Headline", fontname="helv", fontsize=14)
    page.insert_text((50, 100), "Line 2 contains beautiful text", fontname="helv", fontsize=12)
    page.insert_text((50, 150), "Line 3 Footer Note", fontname="helv", fontsize=10)
    doc.save(input_pdf)
    doc.close()

    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Line 1 Top Headline",
                        "original_text": "Line 1 Top Headline",
                        "x": 50, "y": 40, "width": 150, "height": 15,
                    },
                    {
                        "text": "Line 2 contains wonderful text",
                        "original_text": "Line 2 contains beautiful text",
                        "x": 50, "y": 90, "width": 200, "height": 15,
                    },
                    {
                        "text": "Line 3 Footer Note",
                        "original_text": "Line 3 Footer Note",
                        "x": 50, "y": 140, "width": 120, "height": 15,
                    },
                ],
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    mod_doc = fitz.open(output_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    drawings = mod_doc[0].get_drawings()
    mod_doc.close()

    assert "Line 1 Top Headline" in text_out
    assert "Line 2 contains" in text_out
    assert "wonderful" in text_out
    assert "Line 3 Footer Note" in text_out
    assert "beautiful" not in text_out
    assert len(drawings) == 0  # Zero white cover boxes or unnecessary vector paths introduced!


def test_phase3c_backward_compatibility():
    """Verify old payload schemas without style objects compile smoothly."""
    input_pdf = os.path.join(TEMP_DIR, "phase3c_compat_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase3c_compat_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase3c_compat_pages.json")

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Old payload text", fontname="helv", fontsize=12)
    doc.save(input_pdf)
    doc.close()

    old_payload = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "New payload text",
                        "original_text": "Old payload text",
                        "x": 50, "y": 40, "width": 120, "height": 15,
                        "size": 12, "font": "helv", "text_color": "#000000"
                    }
                ]
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(old_payload, f)

    compile_document(input_pdf, output_pdf, pages_json)

    mod_doc = fitz.open(output_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "New" in text_out
    assert "payload text" in text_out
    assert "Old" not in text_out

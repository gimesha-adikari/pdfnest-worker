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


def test_phase3d_true_word_level_style_editing():
    """Verify selecting a specific word ('production-ready') for style editing redacts and re-renders ONLY that word."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Built a production-ready PDF platform", fontname="helv", fontsize=12)

    element = {
        "text": "Built a production-ready PDF platform",
        "original_text": "Built a production-ready PDF platform",
        "target_substring": "production-ready",
        "x": 50, "y": 40, "width": 250, "height": 15,
        "style": {"bold": True, "color": "#FF0000"},
    }

    assert is_element_dirty(element) is True

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1

    target = targets[0]
    assert target["original_substring"] == "production-ready"
    assert target["granularity"] == "word"

    bbox = target["target_bbox"]
    assert bbox[0] > 50  # x0 after 'Built a '
    assert bbox[2] < 220 # x1 before ' PDF platform'

    metrics = render_surgical_replacement(page, target, element=element)
    assert metrics["style"]["bold"] is True
    assert metrics["style"]["font_code"] == "hebo"

    out_pdf = os.path.join(TEMP_DIR, "phase3d_word_style.pdf")
    doc.save(out_pdf)
    doc.close()

    # Re-open and verify text extracted
    mod_doc = fitz.open(out_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Built a" in text_out
    assert "production-ready" in text_out
    assert "PDF platform" in text_out


def test_phase3d_multiple_independent_word_edits_on_one_line():
    """Verify multiple word-level edits on a single line execute as independent surgical operations."""
    input_pdf = os.path.join(TEMP_DIR, "phase3d_multi_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase3d_multi_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase3d_multi_pages.json")

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Built a production-ready PDF platform", fontname="helv", fontsize=12)
    doc.save(input_pdf)
    doc.close()

    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Built a production-ready PDF platform",
                        "original_text": "Built a production-ready PDF platform",
                        "target_substring": "Built",
                        "x": 50, "y": 40, "width": 250, "height": 15,
                        "style": {"bold": True},
                    },
                    {
                        "text": "Built a production-ready PDF platform",
                        "original_text": "Built a production-ready PDF platform",
                        "target_substring": "production-ready",
                        "x": 50, "y": 40, "width": 250, "height": 15,
                        "style": {"color": "#FF0000"},
                    },
                    {
                        "text": "Built a production-ready PDF platform",
                        "original_text": "Built a production-ready PDF platform",
                        "target_substring": "PDF",
                        "x": 50, "y": 40, "width": 250, "height": 15,
                        "style": {"underline": True},
                    },
                ],
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    mod_doc = fitz.open(output_pdf)
    drawings = mod_doc[0].get_drawings()
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Built" in text_out
    assert "production-ready" in text_out
    assert "PDF" in text_out
    assert len(drawings) >= 1  # Underline line drawn for 'PDF'


def test_phase3d_combined_content_and_style_editing():
    """Verify combining word content change ('beautiful' -> 'wonderful') with bold red formatting."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12)

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15,
        "style": {"bold": True, "color": "#FF0000", "underline": True},
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1
    target = targets[0]

    assert target["original_substring"] == "beautiful"
    assert target["replacement_substring"] == "wonderful"

    metrics = render_surgical_replacement(page, target, element=element)
    assert metrics["style"]["bold"] is True
    assert metrics["style"]["color"] == "#FF0000"

    out_pdf = os.path.join(TEMP_DIR, "phase3d_content_style.pdf")
    doc.save(out_pdf)
    doc.close()

    mod_doc = fitz.open(out_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Hello" in text_out
    assert "wonderful" in text_out
    assert "world" in text_out
    assert "beautiful" not in text_out


def test_phase3d_native_vs_ocr_mode_isolation():
    """Verify native PDF page uses mode A (native surgical rendering) while scanned page uses mode B."""
    input_pdf = os.path.join(TEMP_DIR, "phase3d_modes_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase3d_modes_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase3d_modes_pages.json")

    doc = fitz.open()
    # Page 1: Native PDF
    page1 = doc.new_page(width=600, height=800)
    page1.insert_text((50, 50), "Native PDF Text", fontname="helv", fontsize=12)
    # Page 2: Scanned OCR page
    page2 = doc.new_page(width=600, height=800)
    page2.draw_rect(fitz.Rect(0, 0, 600, 800), fill=(0.9, 0.9, 0.9))
    doc.save(input_pdf)
    doc.close()

    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "is_ocr": False,
                "elements": [
                    {
                        "text": "Native PDF Edited",
                        "original_text": "Native PDF Text",
                        "x": 50, "y": 40, "width": 120, "height": 15,
                    }
                ],
            },
            {
                "page_num": 2,
                "is_ocr": True,
                "elements": [
                    {
                        "text": "OCR Text Edited",
                        "original_text": "OCR Text Original",
                        "x": 50, "y": 40, "width": 120, "height": 15,
                        "bg_color": "#e0e0e0",
                    }
                ],
            },
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    mod_doc = fitz.open(output_pdf)
    text_p1 = mod_doc[0].get_text("text").strip()
    images_p2 = mod_doc[1].get_images()
    mod_doc.close()

    assert "Native PDF Edited" in text_p1
    assert len(images_p2) >= 1  # OCR mode re-composited page image

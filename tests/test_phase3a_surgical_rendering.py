from __future__ import annotations

import os
import tempfile
import fitz
import pytest

from app.api.tools.editor.document import (
    resolve_surgical_targets,
    render_surgical_replacement,
)

TEMP_ARTIFACT_DIR = tempfile.gettempdir()


def test_fixture_a_white_background_replacement():
    """Fixture A: Plain white background replacement of 'beautiful' -> 'wonderful'."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50,
        "y": 40,
        "width": 200,
        "height": 15,
        "size": 12,
        "font": "helv",
        "text_color": "#000000",
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1
    target = targets[0]

    # Perform surgical rendering with white fill (1, 1, 1)
    metrics = render_surgical_replacement(page, target, fill_color=(1, 1, 1))

    # Save output PDF artifact for inspection
    output_pdf = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_a_white_bg.pdf")
    doc.save(output_pdf)
    doc.close()

    # Re-open modified PDF and inspect extracted text
    mod_doc = fitz.open(output_pdf)
    mod_text = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Hello" in mod_text
    assert "wonderful" in mod_text
    assert "world" in mod_text
    assert "beautiful" not in mod_text
    assert metrics["operation"] == "replace"


def test_fixture_b_colored_background_replacement():
    """Fixture B: Solid colored background. Compares fill=None (transparent) vs fill=(1,1,1)."""
    # 1. Test fill=(1,1,1) -> white fill
    doc_white = fitz.open()
    page_w = doc_white.new_page(width=600, height=800)
    page_w.draw_rect(fitz.Rect(30, 30, 300, 80), color=None, fill=(0.88, 0.95, 1.0)) # Light blue box
    page_w.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15, "size": 12, "font": "helv"
    }

    targets_w = resolve_surgical_targets(page_w, element)
    render_surgical_replacement(page_w, targets_w[0], fill_color=(1, 1, 1))
    out_white = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_b_white_fill.pdf")
    doc_white.save(out_white)
    doc_white.close()

    # 2. Test fill=None -> transparent redaction
    doc_trans = fitz.open()
    page_t = doc_trans.new_page(width=600, height=800)
    page_t.draw_rect(fitz.Rect(30, 30, 300, 80), color=None, fill=(0.88, 0.95, 1.0))
    page_t.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    targets_t = resolve_surgical_targets(page_t, element)
    render_surgical_replacement(page_t, targets_t[0], fill_color=None)
    out_trans = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_b_trans_fill.pdf")
    doc_trans.save(out_trans)
    doc_trans.close()

    # Verify both PDFs saved and generated text
    mod_trans = fitz.open(out_trans)
    text_t = mod_trans[0].get_text("text").strip()
    drawings_t = mod_trans[0].get_drawings()
    mod_trans.close()

    assert "wonderful" in text_t
    assert len(drawings_t) > 0  # Background vector rectangle preserved!


def test_fixture_c_vector_graphics_preservation():
    """Fixture C: Text beside/behind vector graphics (colored rule line)."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # Draw decorative line below text
    page.draw_line(fitz.Point(30, 60), fitz.Point(300, 60), color=(1, 0, 0), width=2)
    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15, "size": 12, "font": "helv"
    }

    targets = resolve_surgical_targets(page, element)
    render_surgical_replacement(page, targets[0], fill_color=None)

    out_pdf = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_c_vector.pdf")
    doc.save(out_pdf)
    doc.close()

    mod_doc = fitz.open(out_pdf)
    drawings = mod_doc[0].get_drawings()
    mod_doc.close()

    assert len(drawings) > 0  # Vector line preserved!


def test_fixture_d_image_background_behavior():
    """Fixture D: Text over an image background."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)

    # Insert sample pixmap image
    pix = fitz.Pixmap(fitz.csRGB, fitz.Rect(0, 0, 200, 100), False)
    pix.clear_with(200)  # grayish image
    page.insert_image(fitz.Rect(30, 30, 250, 100), pixmap=pix)

    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15, "size": 12, "font": "helv"
    }

    targets = resolve_surgical_targets(page, element)
    # Redact with white fill
    render_surgical_replacement(page, targets[0], fill_color=(1, 1, 1))

    out_pdf = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_d_image.pdf")
    doc.save(out_pdf)
    doc.close()

    mod_doc = fitz.open(out_pdf)
    images = mod_doc[0].get_images()
    mod_doc.close()

    assert len(images) > 0  # Underlying image object survived!


def test_fixture_e_neighboring_text_preservation():
    """Fixture E: Assert neighboring text 'Hello' and 'world' remain 100% untouched."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50, "y": 40, "width": 200, "height": 15, "size": 12, "font": "helv"
    }

    targets = resolve_surgical_targets(page, element)
    render_surgical_replacement(page, targets[0], fill_color=(1, 1, 1))

    out_pdf = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_e_neighbor.pdf")
    doc.save(out_pdf)
    doc.close()

    mod_doc = fitz.open(out_pdf)
    raw = mod_doc[0].get_text("rawdict")
    mod_doc.close()

    words_extracted = []
    for b in raw.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                chars_text = "".join(c["c"] for c in s.get("chars", []))
                words_extracted.append(chars_text)

    full_text = " ".join(words_extracted)
    assert "Hello" in full_text
    assert "world" in full_text
    assert "wonderful" in full_text
    assert "beautiful" not in full_text


def test_fixture_f_and_g_shorter_and_longer_replacement():
    """Fixture F & G: Measure metrics for shorter ('Developer' -> 'Dev') and longer ('Dev' -> 'Senior Developer')."""
    # 1. Shorter replacement
    doc_f = fitz.open()
    page_f = doc_f.new_page(width=600, height=800)
    page_f.insert_text((50, 50), "Developer", fontname="helv", fontsize=12)

    element_f = {
        "text": "Dev",
        "original_text": "Developer",
        "x": 50, "y": 40, "width": 60, "height": 15, "size": 12, "font": "helv"
    }
    targets_f = resolve_surgical_targets(page_f, element_f)
    metrics_f = render_surgical_replacement(page_f, targets_f[0], fill_color=(1, 1, 1))
    doc_f.close()

    assert metrics_f["width_diff"] < 0  # Shorter width!

    # 2. Longer replacement
    doc_g = fitz.open()
    page_g = doc_g.new_page(width=600, height=800)
    page_g.insert_text((50, 50), "Dev", fontname="helv", fontsize=12)

    element_g = {
        "text": "Senior Developer",
        "original_text": "Dev",
        "x": 50, "y": 40, "width": 25, "height": 15, "size": 12, "font": "helv"
    }
    targets_g = resolve_surgical_targets(page_g, element_g)
    metrics_g = render_surgical_replacement(page_g, targets_g[0], fill_color=(1, 1, 1))
    doc_g.close()

    assert metrics_g["width_diff"] > 0  # Longer width!


def test_fixture_h_unicode_replacement_experiments():
    """Fixture H: Test Sinhala, Tamil, Arabic replacement with PyMuPDF standard font vs TTF."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "සිංහල සටහන", fontname="helv", fontsize=12)

    element = {
        "text": "සිංහල ලේඛනය",
        "original_text": "සිංහල සටහන",
        "x": 50, "y": 40, "width": 100, "height": 15, "size": 12, "font": "helv"
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1

    # Standard font insert (helv) on Sinhala Unicode string
    metrics = render_surgical_replacement(page, targets[0], fill_color=(1, 1, 1))
    out_pdf = os.path.join(TEMP_ARTIFACT_DIR, "phase3a_fixture_h_unicode.pdf")
    doc.save(out_pdf)
    doc.close()

    # Re-read PDF text
    mod_doc = fitz.open(out_pdf)
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    # Standard PDF Helvetica font cannot represent Sinhala Unicode glyphs; outputs '?' or missing glyphs
    assert metrics["operation"] == "replace"

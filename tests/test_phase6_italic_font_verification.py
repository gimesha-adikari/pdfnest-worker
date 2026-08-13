from __future__ import annotations

import fitz
import pytest

from app.api.tools.editor.document import resolve_pdf_font_variant, render_surgical_replacement


def test_phase6_italic_font_variant_resolution():
    """Verify font variant resolution returns distinct font codes for regular, bold, italic, and bold-italic."""
    # Helvetica
    assert resolve_pdf_font_variant("helvetica", bold=False, italic=False) == "helv"
    assert resolve_pdf_font_variant("helvetica", bold=True, italic=False) == "hebo"
    assert resolve_pdf_font_variant("helvetica", bold=False, italic=True) == "heit"
    assert resolve_pdf_font_variant("helvetica", bold=True, italic=True) == "hebi"

    # Times
    assert resolve_pdf_font_variant("times", bold=False, italic=False) == "tiro"
    assert resolve_pdf_font_variant("times", bold=True, italic=False) == "tibo"
    assert resolve_pdf_font_variant("times", bold=False, italic=True) == "tiit"
    assert resolve_pdf_font_variant("times", bold=True, italic=True) == "tibi"

    # Courier
    assert resolve_pdf_font_variant("courier", bold=False, italic=False) == "cour"
    assert resolve_pdf_font_variant("courier", bold=True, italic=False) == "cobo"
    assert resolve_pdf_font_variant("courier", bold=False, italic=True) == "coit"
    assert resolve_pdf_font_variant("courier", bold=True, italic=True) == "cobi"


def test_phase6_italic_font_pdf_rendering():
    """Verify that italic text actually renders in PDF without falling back to regular font."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)

    target = {
        "operation": "replace",
        "original_substring": "test",
        "replacement_substring": "Italic Sample",
        "target_bbox": [50, 50, 150, 65],
        "baseline_y": 62,
    }

    element = {
        "text": "Italic Sample",
        "style": {"italic": True, "fontFamily": "helv"},
    }

    metrics = render_surgical_replacement(page, target, element=element)
    assert metrics["style"]["font_code"] == "heit"
    assert metrics["style"]["italic"] is True

    # Bold + Italic
    element_bi = {
        "text": "Bold Italic Sample",
        "style": {"bold": True, "italic": True, "fontFamily": "times"},
    }
    target_bi = {
        "operation": "replace",
        "replacement_substring": "Bold Italic Sample",
        "target_bbox": [50, 100, 200, 115],
        "baseline_y": 112,
    }
    metrics_bi = render_surgical_replacement(page, target_bi, element=element_bi)
    assert metrics_bi["style"]["font_code"] == "tibi"
    doc.close()

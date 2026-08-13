from __future__ import annotations

import json
import os
import tempfile
import fitz
import pytest

from app.api.tools.editor.document import compile_document

TEMP_DIR = tempfile.gettempdir()


def test_phase6_pixel_exact_visual_diff_verification():
    """Perform pixel-exact visual diff analysis comparing original vs edited PDF page outside target edit region."""
    input_pdf = os.path.join(TEMP_DIR, "phase6_vis_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase6_vis_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase6_vis_pages.json")

    # 1. Create multi-line original document with background rectangle
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.draw_rect(fitz.Rect(30, 30, 500, 300), color=None, fill=(0.9, 0.9, 0.9))
    page.insert_text((50, 50), "Line 1 Top Headline", fontname="helv", fontsize=14, color=(0, 0, 0))
    page.insert_text((50, 100), "Line 2 contains beautiful text", fontname="helv", fontsize=12, color=(0, 0, 0))
    page.insert_text((50, 150), "Line 3 Footer Note", fontname="helv", fontsize=10, color=(0, 0, 0))
    doc.save(input_pdf)

    # Render original page to pixmap
    pix_orig = page.get_pixmap(dpi=150)
    doc.close()

    # 2. Layout edit data for 1 word replacement on Line 2
    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Line 2 contains wonderful text",
                        "original_text": "Line 2 contains beautiful text",
                        "target_substring": "beautiful",
                        "x": 50, "y": 90, "width": 200, "height": 15,
                    }
                ],
            }
        ]
    }

    with open(pages_json, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    compile_document(input_pdf, output_pdf, pages_json)

    # 3. Render edited page to pixmap
    mod_doc = fitz.open(output_pdf)
    pix_edited = mod_doc[0].get_pixmap(dpi=150)
    mod_doc.close()

    assert pix_orig.width == pix_edited.width
    assert pix_orig.height == pix_edited.height

    # 4. Compare pixels outside target word bounding box ('beautiful' at DPI 150 scale)
    # Target word 'beautiful' is roughly x=140..200, y=85..110 in page coords
    # Scaled (DPI 150 / 72 = 2.0833): x=290..420, y=170..230 in pixel coords
    dpi_scale = 150.0 / 72.0
    target_px_x0 = int(130 * dpi_scale)
    target_px_x1 = int(220 * dpi_scale)
    target_px_y0 = int(85 * dpi_scale)
    target_px_y1 = int(120 * dpi_scale)

    bytes_orig = pix_orig.samples
    bytes_edited = pix_edited.samples

    total_outside_pixels = 0
    changed_outside_pixels = 0

    w = pix_orig.width
    h = pix_orig.height
    n = pix_orig.n

    for y in range(h):
        for x in range(w):
            if target_px_x0 <= x <= target_px_x1 and target_px_y0 <= y <= target_px_y1:
                continue  # Skip target edit region

            total_outside_pixels += 1
            idx = (y * w + x) * n
            if bytes_orig[idx:idx+n] != bytes_edited[idx:idx+n]:
                changed_outside_pixels += 1

    diff_ratio = (changed_outside_pixels / total_outside_pixels) if total_outside_pixels > 0 else 0.0

    print(f"Visual Diff Result: {changed_outside_pixels} / {total_outside_pixels} changed ({diff_ratio:.4%})")
    assert changed_outside_pixels == 0  # 100% pixel-exact preservation outside target region!

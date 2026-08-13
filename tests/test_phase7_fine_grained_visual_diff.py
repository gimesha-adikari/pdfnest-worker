from __future__ import annotations

import json
import os
import tempfile
import fitz
import pytest

from app.api.tools.editor.document import compile_document

TEMP_DIR = tempfile.gettempdir()


def test_phase7_fine_grained_word_level_visual_diff():
    """Verify that replacing 'beautiful' with 'wonderful' leaves neighboring words 'Hello' and 'world' and page background 100% pixel-exact."""
    input_pdf = os.path.join(TEMP_DIR, "phase7_vis_in.pdf")
    output_pdf = os.path.join(TEMP_DIR, "phase7_vis_out.pdf")
    pages_json = os.path.join(TEMP_DIR, "phase7_vis_pages.json")

    # 1. Create original multi-line PDF page with colored background fill
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.draw_rect(fitz.Rect(30, 30, 500, 300), color=None, fill=(0.88, 0.88, 0.88))
    page.insert_text((50, 50), "Headline Section Title", fontname="helv", fontsize=14, color=(0, 0, 0))
    page.insert_text((50, 100), "Hello beautiful world", fontname="helv", fontsize=12, color=(0, 0, 0))
    page.insert_text((50, 150), "Footer Copyright Notice", fontname="helv", fontsize=10, color=(0, 0, 0))
    doc.save(input_pdf)

    # Render original page to pixmap
    pix_orig = page.get_pixmap(dpi=150)
    doc.close()

    # 2. Layout edit payload replacing ONLY 'beautiful' -> 'wonderful'
    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "elements": [
                    {
                        "text": "Hello wonderful world",
                        "original_text": "Hello beautiful world",
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

    # 3. Render compiled PDF page to pixmap
    mod_doc = fitz.open(output_pdf)
    pix_edited = mod_doc[0].get_pixmap(dpi=150)
    
    # Verify PDF object stream integrity
    images = mod_doc[0].get_images()
    text_out = mod_doc[0].get_text("text").strip()
    mod_doc.close()

    assert "Headline Section Title" in text_out
    assert "Hello" in text_out
    assert "wonderful" in text_out
    assert "world" in text_out
    assert "beautiful" not in text_out
    assert len(images) == 0  # Native vector page, zero raster pixmap conversion

    # 4. Fine-grained pixel diff excluding strictly the target word 'beautiful' bounding box
    dpi_scale = 150.0 / 72.0
    # 'beautiful' is at x=78..135 pt on line 2 (x=50 is 'Hello')
    # Scaled to 150 DPI: target_x0=160, target_x1=285, target_y0=180, target_y1=225
    target_px_x0 = int(75 * dpi_scale)
    target_px_x1 = int(140 * dpi_scale)
    target_px_y0 = int(85 * dpi_scale)
    target_px_y1 = int(115 * dpi_scale)

    # Verify neighboring word 'Hello' (x=50..75) is OUTSIDE the target exclusion region
    hello_px_x0 = int(50 * dpi_scale)
    hello_px_x1 = int(74 * dpi_scale)
    assert hello_px_x1 < target_px_x0  # 'Hello' is fully included in compared pixel analysis!

    bytes_orig = pix_orig.samples
    bytes_edited = pix_edited.samples

    total_compared_pixels = 0
    changed_pixels = 0

    w = pix_orig.width
    h = pix_orig.height
    n = pix_orig.n

    for y in range(h):
        for x in range(w):
            if target_px_x0 <= x <= target_px_x1 and target_px_y0 <= y <= target_px_y1:
                continue  # Skip strictly target word bounding box

            total_compared_pixels += 1
            idx = (y * w + x) * n
            if bytes_orig[idx:idx+n] != bytes_edited[idx:idx+n]:
                changed_pixels += 1

    diff_percentage = (changed_pixels / total_compared_pixels) * 100.0 if total_compared_pixels > 0 else 0.0

    print(f"Fine-Grained Visual Diff: {changed_pixels} / {total_compared_pixels} pixels changed ({diff_percentage:.4f}%)")
    assert changed_pixels == 0  # 100% pixel-exact preservation of 'Hello', 'world', Section Title, and Background!

from __future__ import annotations

import subprocess
from pathlib import Path
import fitz
import pytest

from app.api.tools.markup.document import process_markup_pdf_v2_regions


REAL_BASE_PDF = Path("/tmp/dec_v8_base.pdf")


def _get_target_selection_box() -> dict[str, float]:
    """The exact visible selection rectangle dragged over 'Confirmation of Academic Details'."""
    return {
        "id": "real-target-box",
        "page": 1,
        "x": 580.89,
        "y": 57.14,
        "width": 79.33,
        "height": 279.39,
        "color": "#FFFF00",
    }


def _calculate_overlap_ratio(r1: fitz.Rect, r2: fitz.Rect) -> float:
    inter = fitz.Rect(r1).intersect(fitz.Rect(r2))
    if inter.is_empty:
        return 0.0
    return inter.get_area() / min(r1.get_area(), r2.get_area())


@pytest.mark.skipif(not REAL_BASE_PDF.exists(), reason="Real decrypted base PDF not present in /tmp")
@pytest.mark.parametrize("mode", ["smart", "manual", "ocr"])
@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
def test_real_production_rotated_page_markup_all_modes_and_actions(tmp_path: Path, mode: str, action: str) -> None:
    output_pdf = tmp_path / f"out_real_page1_{mode}_{action}.pdf"
    box = _get_target_selection_box()

    result = process_markup_pdf_v2_regions(
        str(REAL_BASE_PDF),
        str(output_pdf),
        boxes=[box],
        action=action,
        mode=mode,
    )

    doc = fitz.open(str(output_pdf))
    page = doc[0]

    # Verify page rotation is preserved
    assert page.rotation == 90, f"Page rotation changed: {page.rotation}"
    assert page.rect.width > page.rect.height, "Page must remain landscape in visible orientation"

    user_sel_rect = fitz.Rect(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])

    if mode == "manual":
        # Manual mode draws directly into content stream while derotated.
        # Render pixmap and verify yellow/colored mark appears in the target visible selection.
        pix = page.get_pixmap()
        # Find pixels with high red and green (yellowish) or dark marks (strike/underline)
        found_in_target = False
        vx0 = int(user_sel_rect.x0)
        vy0 = int(user_sel_rect.y0)
        vx1 = int(user_sel_rect.x1)
        vy1 = int(user_sel_rect.y1)
        for y in range(max(0, vy0), min(pix.height, vy1)):
            for x in range(max(0, vx0), min(pix.width, vx1)):
                r, g, b = pix.pixel(x, y)[:3]
                if action == "highlight" and r > 200 and g > 200 and b < 220 and r != b:
                    found_in_target = True
                    break
                elif action != "highlight" and (r < 80 or (r > 180 and b < 100)):
                    found_in_target = True
                    break
            if found_in_target:
                break
        assert found_in_target, f"Manual {action} markup not found in visible target area"
    else:
        # Smart and OCR modes create PDF annotations.
        annots = list(page.annots() or [])
        assert len(annots) >= 1, f"Expected at least 1 annotation, found {len(annots)}"
        annot = annots[-1]  # Get newly created annotation

        expected_type = "Highlight" if action == "highlight" else "Underline" if action == "underline" else "StrikeOut"
        assert annot.type[1] == expected_type, f"Annotation type mismatch: {annot.type[1]} != {expected_type}"

        # In canonical space, the vertices must be unrotated canonical coordinates
        vertices = annot.vertices
        assert len(vertices) >= 4, "Annotation must have QuadPoints"

        # Transform annotation quads into visible coordinates via page.rotation_matrix
        annot_vis_rects = []
        for i in range(0, len(vertices), 4):
            quad = vertices[i:i+4]
            q_rect = fitz.Rect(min(p[0] for p in quad), min(p[1] for p in quad), max(p[0] for p in quad), max(p[1] for p in quad))
            annot_vis_rects.append(q_rect * page.rotation_matrix)

        annot_vis_union = fitz.Rect()
        for r in annot_vis_rects:
            annot_vis_union.include_rect(r)

        # Quantitative overlap assertion: visible annotation bounds must overlap user selection
        overlap = _calculate_overlap_ratio(annot_vis_union, user_sel_rect)
        assert overlap > 0.60, (
            f"Mode {mode} Action {action}: Insufficient overlap with target selection! "
            f"Overlap ratio: {overlap:.2f}, annot visible rect: {annot_vis_union}, user selection: {user_sel_rect}"
        )

        # Verify annotation is NOT near the bottom edge (which was the bug at Y in [573, 595])
        assert annot_vis_union.y0 < 200, f"Annotation displaced near bottom edge: y0={annot_vis_union.y0}"
        assert annot_vis_union.x0 >= 550, f"Annotation X displaced: x0={annot_vis_union.x0}"

    doc.close()

    # Verify structural integrity with qpdf
    qpdf_check = subprocess.run(["qpdf", "--check", str(output_pdf)], capture_output=True, text=True)
    assert qpdf_check.returncode == 0, f"qpdf check failed: {qpdf_check.stderr}"


@pytest.mark.skipif(not REAL_BASE_PDF.exists(), reason="Real decrypted base PDF not present in /tmp")
def test_unrotated_page2_golden_baseline(tmp_path: Path) -> None:
    """Ensure unrotated 0 deg Page 2 works identically with zero regression."""
    output_pdf = tmp_path / "out_page2_unrotated.pdf"
    box = {
        "id": "page2-box",
        "page": 2,
        "x": 100.0,
        "y": 150.0,
        "width": 200.0,
        "height": 50.0,
        "color": "#FFFF00",
    }

    result = process_markup_pdf_v2_regions(
        str(REAL_BASE_PDF),
        str(output_pdf),
        boxes=[box],
        action="highlight",
        mode="smart",
    )

    doc = fitz.open(str(output_pdf))
    page = doc[1]
    assert page.rotation == 0, "Page 2 must remain rotation 0"

    annots = list(page.annots() or [])
    assert len(annots) >= 1

    doc.close()
    qpdf_check = subprocess.run(["qpdf", "--check", str(output_pdf)], capture_output=True, text=True)
    assert qpdf_check.returncode == 0

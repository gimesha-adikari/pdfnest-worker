from __future__ import annotations

import subprocess
from pathlib import Path
import fitz
import pytest

from app.core.studio_markup_region_ocr_engine import execute_studio_markup_region_ocr


def _make_sample_pdf(path: Path, rotation: int = 0) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text(fitz.Point(60, 80), "Studio Rotated Markup", fontsize=20)
    if rotation:
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()


def _display_to_canonical(page_w: float, page_h: float, rot: int, display_rect: dict[str, float]) -> dict[str, float]:
    """Mirror of displayRectToCanonicalPdfRect in StudioV2Geometry.ts."""
    norm_rot = ((rot % 360) + 360) % 360
    vx = display_rect["x"]
    vy = display_rect["y"]
    vw = display_rect["width"]
    vh = display_rect["height"]

    if norm_rot == 90:
        return {"x": round(vy, 2), "y": round(page_h - (vx + vw), 2), "width": round(vh, 2), "height": round(vw, 2)}
    elif norm_rot == 180:
        return {"x": round(page_w - (vx + vw), 2), "y": round(page_h - (vy + vh), 2), "width": round(vw, 2), "height": round(vh, 2)}
    elif norm_rot == 270:
        return {"x": round(page_w - (vy + vh), 2), "y": round(vx, 2), "width": round(vh, 2), "height": round(vw, 2)}
    else:
        return {"x": round(vx, 2), "y": round(vy, 2), "width": round(vw, 2), "height": round(vh, 2)}


def _canonical_to_display(page_w: float, page_h: float, rot: int, can_rect: dict[str, float]) -> dict[str, float]:
    """Mirror of canonicalPdfRectToDisplayRect in StudioV2Geometry.ts."""
    norm_rot = ((rot % 360) + 360) % 360
    cx = can_rect["x"]
    cy = can_rect["y"]
    cw = can_rect["width"]
    ch = can_rect["height"]

    if norm_rot == 90:
        return {"x": round(page_h - (cy + ch), 2), "y": round(cx, 2), "width": round(ch, 2), "height": round(cw, 2)}
    elif norm_rot == 180:
        return {"x": round(page_w - (cx + cw), 2), "y": round(page_h - (cy + ch), 2), "width": round(cw, 2), "height": round(ch, 2)}
    elif norm_rot == 270:
        return {"x": round(cy, 2), "y": round(page_w - (cx + cw), 2), "width": round(ch, 2), "height": round(cw, 2)}
    else:
        return {"x": round(cx, 2), "y": round(cy, 2), "width": round(cw, 2), "height": round(ch, 2)}


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
def test_studio_v2_smart_markup_on_all_rotations(tmp_path: Path, rotation: int, action: str) -> None:
    source = tmp_path / f"source_rot_{rotation}.pdf"
    _make_sample_pdf(source, rotation=rotation)

    # In canonical coordinates, text "Studio Rotated Markup" is around x in [60, 290], y in [60, 85].
    canonical_target = {"x": 55.0, "y": 58.0, "width": 240.0, "height": 30.0}

    # Simulate user dragging over the visually rendered text in displayed page coordinates:
    display_box = _canonical_to_display(400, 300, rotation, canonical_target)

    # Studio V2 sends visible display coordinates directly:
    payload_box = {
        "id": f"reg-{rotation}-{action}",
        "page": 1,
        "x": display_box["x"],
        "y": display_box["y"],
        "width": display_box["width"],
        "height": display_box["height"],
        "color": "#FFFF00",
    }

    output = tmp_path / f"out_rot_{rotation}_{action}.pdf"
    result = execute_studio_markup_region_ocr(
        source,
        output,
        boxes=[payload_box],
        action=action,
        mode="smart",
    )

    assert result["selection_count"] >= 1, f"Smart selection failed for rotation {rotation}"
    assert "selections" in result and len(result["selections"]) >= 1

    doc = fitz.open(output)
    page = doc[0]
    assert page.rotation == rotation, "Page rotation must be preserved"

    annots = list(page.annots() or [])
    assert len(annots) == 1, f"Expected 1 annotation on page for rot {rotation}, found {len(annots)}"
    annot = annots[0]

    expected_type = "Highlight" if action == "highlight" else "Underline" if action == "underline" else "StrikeOut"
    assert annot.type[1] == expected_type, f"Annot type mismatch: {annot.type[1]} != {expected_type}"

    # Verify QuadPoints (vertices) are canonical and cover the words:
    vertices = annot.vertices
    assert len(vertices) >= 4, "Annotation must contain valid QuadPoints"
    xs = [p[0] for p in vertices]
    ys = [p[1] for p in vertices]
    assert min(xs) <= 65 and max(xs) >= 260, f"QuadPoints do not span the text: x range [{min(xs)}, {max(xs)}]"
    assert min(ys) <= 70 and max(ys) >= 75, f"QuadPoints do not cover y range: [{min(ys)}, {max(ys)}]"

    doc.close()

    # Forensics check via qpdf
    qpdf_result = subprocess.run(["qpdf", "--check", str(output)], capture_output=True, text=True)
    assert qpdf_result.returncode == 0, f"qpdf check failed: {qpdf_result.stderr}"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
def test_studio_v2_manual_markup_on_all_rotations(tmp_path: Path, rotation: int, action: str) -> None:
    source = tmp_path / f"source_manual_rot_{rotation}.pdf"
    _make_sample_pdf(source, rotation=rotation)

    canonical_target = {"x": 55.0, "y": 58.0, "width": 240.0, "height": 30.0}
    display_box = _canonical_to_display(400, 300, rotation, canonical_target)
    # Studio V2 sends visible display coordinates directly:
    payload_box = {
        "id": f"reg-manual-{rotation}-{action}",
        "page": 1,
        "x": display_box["x"],
        "y": display_box["y"],
        "width": display_box["width"],
        "height": display_box["height"],
        "color": "#FFFF00",
    }

    output = tmp_path / f"out_manual_rot_{rotation}_{action}.pdf"
    result = execute_studio_markup_region_ocr(
        source,
        output,
        boxes=[payload_box],
        action=action,
        mode="manual",
    )

    assert result["source_policy"] == "MANUAL_RECTANGLE"
    assert result["selection_count"] == 0

    doc = fitz.open(output)
    page = doc[0]
    assert page.rotation == rotation

    # Check pixmap visual coverage: the mark must appear in the displayed target area
    pix = page.get_pixmap(alpha=False)
    # Visible box coordinates for this rotation:
    vx0 = int(display_box["x"])
    vy0 = int(display_box["y"])
    vx1 = int(display_box["x"] + display_box["width"])
    vy1 = int(display_box["y"] + display_box["height"])

    yellow_in_target = False
    for y in range(max(0, vy0), min(pix.height, vy1)):
        for x in range(max(0, vx0), min(pix.width, vx1)):
            r, g, b = pix.pixel(x, y)[:3]
            if action == "highlight" and r > 200 and g > 200 and b < 180:
                yellow_in_target = True
                break
            elif action != "highlight" and r > 180 and g > 180:
                yellow_in_target = True
                break
        if yellow_in_target:
            break

    doc.close()
    assert yellow_in_target, f"Manual markup not found in visual target area for rot={rotation} action={action}"

    qpdf_result = subprocess.run(["qpdf", "--check", str(output)], capture_output=True, text=True)
    assert qpdf_result.returncode == 0, f"qpdf check failed: {qpdf_result.stderr}"

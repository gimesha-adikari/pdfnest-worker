from __future__ import annotations

import io
import json
from pathlib import Path

import pymupdf as fitz
from PIL import Image
from PIL import ImageDraw

from app.api.tools.editor.document import compile_document
from app.api.tools.markup.document import process_markup_pdf


def _rotated_mixed_pdf(path: Path) -> tuple[dict, fitz.Rect]:
    """Create a small rotated image/OCR-layer page using Studio coordinates."""
    image = Image.new("RGB", (300, 500), "white")
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="PNG")
    image.close()

    document = fitz.open()
    page = document.new_page(width=300, height=500)
    page.insert_image(page.rect, stream=image_bytes.getvalue())
    page.insert_text((60, 120), "SourceTitle", fontsize=12, fontname="helv")
    page.set_rotation(90)

    source_words = [word for word in page.get_text("words") if word[4] == "SourceTitle"]
    assert len(source_words) == 1
    native_rect = fitz.Rect(source_words[0][:4])
    visible_rect = native_rect * page.rotation_matrix
    layout = {
        "schema_version": "ocr_v2_editor_layout.v1",
        "ocr_v2": True,
        "success": True,
        "geometry_space": "studio_visible",
        "pages": [
            {
                "page_num": 1,
                "width": page.rect.width,
                "height": page.rect.height,
                "kind": "mixed",
                "is_ocr": True,
                "elements": [
                    {
                        "id": "p1-studio-line-1",
                        "text": "SourceTitle-Audit",
                        "original_text": "SourceTitle",
                        "x": visible_rect.x0,
                        "y": visible_rect.y0,
                        "width": visible_rect.width,
                        "height": visible_rect.height,
                        "size": 12,
                        "font": "helv",
                        "bg_color": "transparent",
                        "text_color": "#000000",
                        "transparent_bg": True,
                    }
                ],
            }
        ],
    }
    document.save(path)
    document.close()
    return layout, visible_rect


def _visible_page_rgb(page: fitz.Page) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _mean_channel_delta(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return sum(abs(a - b) for a, b in zip(left, right)) / 3.0


def test_studio_visible_rotated_ocr_rebuild_preserves_background_orientation(tmp_path: Path) -> None:
    """A visible raster must not be inserted into a still-rotated page twice."""
    source = tmp_path / "asymmetric-rotated-source.pdf"
    layout_path = tmp_path / "asymmetric-studio-layout.json"
    output = tmp_path / "asymmetric-rotated-output.pdf"
    combined = tmp_path / "asymmetric-rotated-combined.pdf"

    # Distinct, asymmetric corner colours make a 90/180/270-degree error
    # visible even when text extraction and /Rotate metadata still look valid.
    image = Image.new("RGB", (300, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 74, 124), fill="#df3030")
    draw.rectangle((225, 0, 299, 124), fill="#2f76dd")
    draw.rectangle((0, 375, 74, 499), fill="#38a64a")
    draw.rectangle((225, 375, 299, 499), fill="#e4bc2d")
    draw.text((95, 230), "Before", fill="black")
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="PNG")
    image.close()

    document = fitz.open()
    page = document.new_page(width=300, height=500)
    page.insert_image(page.mediabox, stream=image_bytes.getvalue())
    page.set_rotation(90)
    # Center mask deliberately avoids the asymmetric corner sentinels.
    layout = {
        "schema_version": "ocr_v2_editor_layout.v1",
        "ocr_v2": True,
        "success": True,
        "geometry_space": "studio_visible",
        "pages": [{
            "page_num": 1,
            "width": page.rect.width,
            "height": page.rect.height,
            "kind": "scanned",
            "is_ocr": True,
            "elements": [{
                "id": "asymmetric-line",
                "text": "After",
                "original_text": "Before",
                "x": 230,
                "y": 235,
                "width": 64,
                "height": 24,
                "size": 12,
                "font": "helv",
                "bg_color": "#ffffff",
                "text_color": "#000000",
            }],
        }],
    }
    document.save(source)
    document.close()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    compile_document(str(source), str(output), str(layout_path))
    # The next Studio mutation must consume the editor result without
    # reintroducing a visible/native coordinate-space mismatch.
    process_markup_pdf(
        str(output),
        str(combined),
        [{"id": "combined-highlight", "page": 1, "x": 180, "y": 110, "width": 100, "height": 28, "color": "#FFFF00"}],
        "highlight",
        mode="manual",
    )

    with fitz.open(source) as source_doc, fitz.open(combined) as output_doc:
        source_page = source_doc[0]
        output_page = output_doc[0]
        assert output_page.rotation == source_page.rotation == 90
        assert output_page.mediabox == source_page.mediabox
        assert output_page.cropbox == source_page.cropbox
        assert output_page.rect == source_page.rect
        assert len(output_page.get_drawings()) >= 1, "the combined markup must be retained"

        source_image = _visible_page_rgb(source_page)
        output_image = _visible_page_rgb(output_page)
        assert output_image.size == source_image.size == (500, 300)

        # Samples are deliberately outside the edited middle region. JPEG
        # re-encoding is allowed a small tolerance, rotation is not.
        for point in ((25, 25), (475, 25), (25, 275), (475, 275)):
            assert _mean_channel_delta(source_image.getpixel(point), output_image.getpixel(point)) < 12

        source_image.close()
        output_image.close()


def test_studio_visible_rotated_ocr_edit_is_materialized_in_native_pdf(tmp_path: Path) -> None:
    source = tmp_path / "rotated-mixed-source.pdf"
    layout_path = tmp_path / "studio-layout.json"
    output = tmp_path / "compiled.pdf"
    layout, visible_rect = _rotated_mixed_pdf(source)
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    compile_document(str(source), str(output), str(layout_path))

    with fitz.open(output) as document:
        page = document[0]
        assert page.rotation == 90
        text = page.get_text("text")
        assert "SourceTitle-Audit" in text
        assert "SourceTitle\n" not in text

        replacement = next(word for word in page.get_text("words") if "SourceTitle-Audit" in word[4])
        replacement_visible = fitz.Rect(replacement[:4]) * page.rotation_matrix
        assert replacement_visible.intersects(visible_rect)
        assert replacement_visible.x0 == fitz.Rect(visible_rect).x0 or replacement_visible.x1 >= visible_rect.x0


def test_studio_visible_rotated_native_edit_uses_the_same_boundary_transform(tmp_path: Path) -> None:
    source = tmp_path / "rotated-native-source.pdf"
    layout_path = tmp_path / "studio-native-layout.json"
    output = tmp_path / "compiled-native.pdf"

    document = fitz.open()
    page = document.new_page(width=300, height=500)
    page.insert_text((60, 120), "SourceTitle", fontsize=12, fontname="helv")
    page.set_rotation(90)
    native_word = next(word for word in page.get_text("words") if word[4] == "SourceTitle")
    visible_rect = fitz.Rect(native_word[:4]) * page.rotation_matrix
    layout = {
        "schema_version": "ocr_v2_editor_layout.v1",
        "ocr_v2": True,
        "success": True,
        "geometry_space": "studio_visible",
        "pages": [
            {
                "page_num": 1,
                "width": page.rect.width,
                "height": page.rect.height,
                "kind": "text",
                "is_ocr": False,
                "elements": [
                    {
                        "id": "p1-studio-native-line-1",
                        "text": "SourceTitle-Audit",
                        "original_text": "SourceTitle",
                        "x": visible_rect.x0,
                        "y": visible_rect.y0,
                        "width": visible_rect.width,
                        "height": visible_rect.height,
                        "size": 12,
                        "font": "helv",
                        "bg_color": "transparent",
                        "text_color": "#000000",
                        "transparent_bg": True,
                    }
                ],
            }
        ],
    }
    document.save(source)
    document.close()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    compile_document(str(source), str(output), str(layout_path))

    with fitz.open(output) as document:
        assert document[0].rotation == 90
        text = document[0].get_text("text")
        assert "SourceTitle-Audit" in text
        assert "SourceTitle\n" not in text


def test_studio_visible_editor_then_markup_preserves_both_operations(tmp_path: Path) -> None:
    source = tmp_path / "rotated-combined-source.pdf"
    editor_output = tmp_path / "rotated-editor.pdf"
    output = tmp_path / "rotated-combined.pdf"
    layout, visible_rect = _rotated_mixed_pdf(source)
    layout_path = tmp_path / "studio-combined-layout.json"
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    compile_document(str(source), str(editor_output), str(layout_path))
    process_markup_pdf(
        str(editor_output),
        str(output),
        [
            {
                "id": "combined-page1-highlight",
                "page": 1,
                "x": max(0.0, visible_rect.x0 - 2.0),
                "y": max(0.0, visible_rect.y0 - 2.0),
                "width": visible_rect.width + 4.0,
                "height": visible_rect.height + 4.0,
                "color": "#FFFF00",
            }
        ],
        action="highlight",
        mode="manual",
    )

    with fitz.open(output) as document:
        page = document[0]
        assert page.rotation == 90
        text = page.get_text("text")
        assert "SourceTitle-Audit" in text
        assert "SourceTitle\n" not in text
        assert any(
            drawing.get("fill")
            and drawing["fill"][0] > 0.9
            and drawing["fill"][1] > 0.9
            and drawing["fill"][2] < 0.1
            for drawing in page.get_drawings()
        )

from __future__ import annotations

import io
import json
from pathlib import Path

import pymupdf as fitz
from PIL import Image

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

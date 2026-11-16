import copy
import json

import fitz
import pytest

from app.core.editor_ocr_projection import (
    merge_native_editor_colors,
    native_editor_color_probe,
    preserve_native_editor_colors,
)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("visible", [False, True])
def test_native_editor_color_survives_rotation_without_geometry_changes(tmp_path, rotation, visible):
    path = tmp_path / "colored.pdf"
    with fitz.open() as document:
        page = document.new_page(width=320, height=450)
        page.set_cropbox(fitz.Rect(10, 20, 300, 430))
        page.insert_text((40, 80), "White source", color=(1, 1, 1), fontsize=16)
        page.set_rotation(rotation)
        span = page.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
        rect = fitz.Rect(span["bbox"])
        if visible:
            rect = rect * page.rotation_matrix
        document.save(path)
    element = {"x": rect.x0, "y": rect.y0, "width": rect.width, "height": rect.height,
               "text": "White source", "text_color": "#000000"}
    layout = {"pages": [{"page_num": 1, "source": "NATIVE_EXTRACTION", "elements": [element]}]}
    before = copy.deepcopy(element)
    preserve_native_editor_colors(layout, str(path), visible_geometry=visible)
    assert element == {**before, "text_color": "#ffffff"}


def test_ocr_colors_are_not_replaced_by_native_styles():
    layout = {"pages": [{"page_num": 1, "source": "OCR_RECOGNITION", "elements": [{"text_color": "#112233"}]}]}
    before = copy.deepcopy(layout)
    preserve_native_editor_colors(layout, "unused-scanned-input.pdf")
    assert layout == before


def test_native_color_probe_omits_large_word_geometry_and_merges_only_color():
    word_geometry = [
        {"id": f"word-{index}", "text": "Education", "x": 12.25, "y": 20.5,
         "width": 42.75, "height": 10.0}
        for index in range(200_000)
    ]
    element = {"id": "line-1", "x": 12.0, "y": 19.0, "width": 500.0, "height": 12.0,
               "text": "Education report", "text_color": "#000000", "word_geometry": word_geometry}
    layout = {"pages": [{"page_num": 1, "source": "NATIVE_EXTRACTION", "elements": [element]}]}
    assert len(json.dumps(layout, ensure_ascii=True).encode("ascii")) > 16 * 1024 * 1024

    probe = native_editor_color_probe(layout)

    assert probe == {"pages": [{"page_num": 1, "source": "NATIVE_EXTRACTION", "elements": [
        {"id": "line-1", "x": 12.0, "y": 19.0, "width": 500.0, "height": 12.0}
    ]}]}
    assert len(json.dumps(probe, ensure_ascii=True).encode("ascii")) < 1024
    color_result = copy.deepcopy(probe)
    color_result["pages"][0]["elements"][0]["text_color"] = "#123456"

    merged = merge_native_editor_colors(layout, color_result)

    assert merged is layout
    assert element["text_color"] == "#123456"
    assert element["word_geometry"] is word_geometry
    assert len(element["word_geometry"]) == 200_000

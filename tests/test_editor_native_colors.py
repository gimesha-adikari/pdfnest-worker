import copy

import fitz
import pytest

from app.core.editor_ocr_projection import preserve_native_editor_colors


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

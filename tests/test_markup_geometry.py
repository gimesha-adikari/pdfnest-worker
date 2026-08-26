import fitz
import pytest

from app.api.tools.markup.document import apply_markup


@pytest.mark.parametrize(
    ("rotation", "action", "color"),
    [
        (0, "highlight", "#FFFF00"),
        (0, "underline", "#FF4D4D"),
        (0, "strikeout", "#FF0000"),
        (90, "highlight", "#FFFF00"),
        (90, "underline", "#FF4D4D"),
        (90, "strikeout", "#FF0000"),
        (180, "highlight", "#FFFF00"),
        (180, "underline", "#FF4D4D"),
        (180, "strikeout", "#FF0000"),
        (270, "highlight", "#FFFF00"),
        (270, "underline", "#FF4D4D"),
        (270, "strikeout", "#FF0000"),
    ],
)
def test_manual_markup_uses_visible_coordinates_for_cropped_rotated_page(tmp_path, rotation, action, color):
    document = fitz.open()
    page = document.new_page(width=595.28, height=841.89)
    page.set_cropbox(fitz.Rect(100, 100, 450, 700))
    page.set_rotation(rotation)

    # This is a visible-page rectangle, crop-relative for every page rotation.
    visible_box = {"x": 72, "y": 56, "width": 276, "height": 42, "page": 1, "color": color}
    apply_markup(document, [visible_box], action=action, mode="manual")
    output = tmp_path / f"{action}.pdf"
    document.save(output)
    document.close()

    rendered = fitz.open(output)[0].get_pixmap(alpha=False)
    # The mark must remain in the selected visible region after crop + 90°.
    samples = []
    for y in range(56, 99):
        for x in range(72, 349):
            red, green, blue = rendered.pixel(x, y)
            if action == "highlight" and red > 220 and green > 220 and blue < 220:
                samples.append((x, y))
            if action != "highlight" and red > 150 and green < 170 and blue < 190:
                samples.append((x, y))
    assert samples, f"{action} pixels were not inside the visible selected region"

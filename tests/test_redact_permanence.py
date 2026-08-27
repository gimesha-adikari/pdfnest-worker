import fitz
from PIL import Image

from app.api.tools.redact.document import redact_pdf
from app.api.tools.redact.models import RedactBox


def create_fixture(path):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((80, 220), "PUBLIC SECRET PUBLIC", fontsize=24)
    doc.save(path)
    doc.close()


def extract_text(path):
    doc = fitz.open(path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text


def test_manual_area_redaction_removes_text_and_preserves_neighbors(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    create_fixture(source)
    source_doc = fitz.open(source)
    secret_rect = source_doc[0].search_for("SECRET")[0]
    page_rect = source_doc[0].rect
    source_doc.close()

    redact_pdf(
        str(source),
        str(output),
        [],
        [RedactBox(page=1, x=secret_rect.x0 / page_rect.width, y=secret_rect.y0 / page_rect.height, width=secret_rect.width / page_rect.width, height=secret_rect.height / page_rect.height)],
    )

    text = extract_text(output)
    assert "SECRET" not in text
    assert "PUBLIC" in text


def test_keyword_redaction_remains_permanent_after_reopen(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    create_fixture(source)
    redact_pdf(str(source), str(output), ["SECRET"], [])
    assert "SECRET" not in extract_text(output)


def test_manual_area_redaction_removes_image_content(tmp_path):
    source = tmp_path / "image-source.pdf"
    output = tmp_path / "image-output.pdf"
    image_path = tmp_path / "green.png"
    Image.new("RGB", (200, 200), "#00ff00").save(image_path)
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_image(fitz.Rect(0, 0, 200, 200), filename=str(image_path))
    doc.save(source)
    doc.close()

    redact_pdf(str(source), str(output), [], [RedactBox(page=1, x=0, y=0.5, width=0.5, height=0.5)])
    rendered = fitz.open(output)[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    redacted_pixel = rendered.pixel(50, 300)
    retained_pixel = rendered.pixel(100, 100)
    assert max(redacted_pixel) < 40
    assert retained_pixel[1] > 150


def test_rotated_cropped_manual_area_redaction_is_permanent(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.set_cropbox(fitz.Rect(50, 100, 550, 700))
    page.set_rotation(90)
    page.insert_text((100, 200), "ROTATED-SECRET", fontsize=20)
    doc.save(source)
    doc.close()

    source_doc = fitz.open(source)
    page = source_doc[0]
    rect = page.search_for("ROTATED-SECRET")[0]
    # search_for is in the unrotated page context; map its corners to the
    # visible rotated crop before submitting the normalized box.
    visible = rect * page.rotation_matrix
    visible_rect = fitz.Rect(visible).normalize()
    page_rect = page.rect
    source_doc.close()
    redact_pdf(str(source), str(output), [], [RedactBox(page=1, x=visible_rect.x0 / page_rect.width, y=visible_rect.y0 / page_rect.height, width=visible_rect.width / page_rect.width, height=visible_rect.height / page_rect.height)])

    assert "ROTATED-SECRET" not in extract_text(output)


def test_invalid_manual_box_is_rejected(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    create_fixture(source)
    try:
        redact_pdf(str(source), str(output), [], [RedactBox(page=1, x=0.9, y=0, width=0.2, height=0.1)])
    except ValueError as error:
        assert "normalized" in str(error)
    else:
        raise AssertionError("out-of-bounds redaction box was accepted")

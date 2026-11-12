from __future__ import annotations

from io import BytesIO
import hashlib
import math
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
import pytest

from app.core.scanned_word_compiler import compile_scanned_word_edits


PROTOCOL = "platen_word_edits.v1"


def _fixture_pdf(path: Path, *, texture: bool = False, border: bool = False, stamp: bool = False, rotation: int = 0, crop: bool = False, hidden_ocr: bool = False, lossy: bool = False, skew: float = 0):
    width_pt, height_pt, scale = 595, 842, 3
    image = Image.new("RGB", (width_pt * scale, height_pt * scale), (247, 245, 238) if texture else "white")
    if texture:
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                shade = (x * 3 + y * 2) % 7 - 3
                pixels[x, y] = tuple(max(0, min(255, channel + shade)) for channel in pixels[x, y])
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 51)
    origin = (120 * scale, 52 * scale)
    draw.text(origin, "Ministry of Education, Higher", font=font, fill=(28, 28, 28))
    target_px = draw.textbbox(origin, "Ministry", font=font)
    if border:
        draw.line((target_px[0], target_px[1] + 2, target_px[2], target_px[1] + 2), fill=(25, 25, 25), width=3)
    if stamp:
        draw.ellipse((target_px[0] + 12, target_px[1] + 4, target_px[0] + 54, target_px[1] + 38), outline=(150, 20, 20), width=5)
    x0, y0, x1, y1 = (value / scale for value in target_px)
    if skew:
        angle = math.radians(skew)
        center_x, center_y = image.width / 2, image.height / 2
        transformed = []
        for px, py in ((target_px[0], target_px[1]), (target_px[2], target_px[1]), (target_px[0], target_px[3]), (target_px[2], target_px[3])):
            dx, dy = px - center_x, py - center_y
            transformed.append((center_x + math.cos(angle) * dx + math.sin(angle) * dy, center_y - math.sin(angle) * dx + math.cos(angle) * dy))
        image = image.rotate(skew, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
        x0, y0 = min(p[0] for p in transformed) / scale, min(p[1] for p in transformed) / scale
        x1, y1 = max(p[0] for p in transformed) / scale, max(p[1] for p in transformed) / scale
    stream = BytesIO()
    image.save(stream, format="JPEG" if lossy else "PNG", quality=68 if lossy else None)
    doc = fitz.open()
    page = doc.new_page(width=width_pt, height=height_pt)
    page.insert_image(page.rect, stream=stream.getvalue())
    if hidden_ocr:
        page.insert_text(
            fitz.Point(x0, y0 + (y1 - y0) * 0.8),
            "Ministry of Education, Higher",
            fontname="helv",
            fontsize=(y1 - y0) * 0.8,
            render_mode=3,
        )
    if rotation:
        page.set_rotation(rotation)
    if crop:
        page.set_cropbox(fitz.Rect(10, 10, width_pt - 10, height_pt - 10))
    doc.save(path)
    doc.close()
    return [x0, y0, x1, y1]


def _request(
    bbox: list[float],
    replacement: str = "hello",
    *,
    page: int = 1,
    word_id: str = "word-1",
    expected: str = "Ministry",
):
    edit = {
        "page_num": page,
        "element_id": "line-1",
        "word_id": word_id,
        "expected_original_text": expected,
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    source_sha = "0" * 64
    digest = "sha256:" + "1" * 64
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": digest,
    }]
    return envelope, resolved, source_sha


def _native_fixture_pdf(path: Path, text: str, *, start: tuple[float, float] = (60, 100), fontname: str = "helv") -> list[list[float]]:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_line((40, 150), (220, 150), color=(0.1, 0.2, 0.8), width=0.5)
    if fontname == "custom":
        fontname = "DejaVuFixture"
        page.insert_font(fontname=fontname, fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    page.insert_text(fitz.Point(*start), text, fontname=fontname, fontsize=14, color=(0.1, 0.2, 0.3))
    boxes = [[float(value) for value in word[:4]] for word in page.get_text("words")]
    doc.save(path)
    doc.close()
    return boxes


def _mixed_native_scan_pdf(path: Path, *, stamp: bool = False) -> tuple[list[float], list[float]]:
    scale = 3
    image = Image.new("RGB", (300 * scale, 100 * scale), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
    image_origin = (54, 45)
    draw.text(image_origin, "Ministry of Education, Higher", font=font, fill=(28, 28, 28))
    ministry = draw.textbbox(image_origin, "Ministry", font=font)
    if stamp:
        draw.ellipse((ministry[0] + 10, ministry[1], ministry[0] + 55, ministry[3]), outline=(180, 0, 0), width=5)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((30, 80), "Notice", fontname="helv", fontsize=14)
    native = [float(value) for value in page.get_text("words")[0][:4]]
    scan_rect = fitz.Rect(100, 150, 400, 250)
    page.insert_image(scan_rect, stream=buffer.getvalue())
    target = [
        scan_rect.x0 + ministry[0] / scale,
        scan_rect.y0 + ministry[1] / scale,
        scan_rect.x0 + ministry[2] / scale,
        scan_rect.y0 + ministry[3] / scale,
    ]
    page.insert_text(
        fitz.Point(target[0], target[1] + (target[3] - target[1]) * 0.8),
        "Ministry of Education, Higher",
        fontname="helv",
        fontsize=(target[3] - target[1]) * 0.8,
        render_mode=3,
    )
    doc.save(path)
    doc.close()
    return native, target


def _two_page_fixture(path: Path, tmp_path: Path, *, second_stamp: bool = False):
    first_path, second_path = tmp_path / "first-page.pdf", tmp_path / "second-page.pdf"
    first_bbox = _fixture_pdf(first_path)
    second_bbox = _fixture_pdf(second_path, stamp=second_stamp)
    doc = fitz.open()
    for item in (first_path, second_path):
        with fitz.open(item) as page_doc:
            doc.insert_pdf(page_doc)
    doc.save(path)
    doc.close()
    return [first_bbox, second_bbox]


def _render(page: fitz.Page, dpi: int = 144) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@pytest.mark.parametrize("texture", [False, True])
def test_compiles_localized_replacement_and_preserves_neighbor_and_scan_image(tmp_path, texture):
    source = tmp_path / "source.pdf"
    output = tmp_path / "candidate.pdf"
    bbox = _fixture_pdf(source, texture=texture)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED"
    assert output.is_file() and output.stat().st_size > 0
    with fitz.open(source) as original, fitz.open(output) as edited:
        assert len(edited) == len(original) == 1
        assert edited[0].rect == original[0].rect
        assert "hello" in edited[0].get_text()
        assert "Ministry" not in edited[0].get_text()
        assert result["image_streams_preserved"] is True
        before, after = _render(original[0]), _render(edited[0])
        assert before.size == after.size
        assert result["outside_changed_pixels_144dpi"] == 0


def test_removes_aligned_hidden_ocr_and_rejects_visible_text_conflict(tmp_path):
    source = tmp_path / "hidden-source.pdf"
    output = tmp_path / "hidden-candidate.pdf"
    bbox = _fixture_pdf(source, hidden_ocr=True)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha
    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)
    assert result["status"] == "EDITED"
    with fitz.open(output) as edited:
        text = edited[0].get_text()
        assert text.count("hello") == 1
        assert "Ministry" not in text

    visible_source = tmp_path / "visible-source.pdf"
    visible_output = tmp_path / "visible-candidate.pdf"
    bbox = _fixture_pdf(visible_source)
    with fitz.open(visible_source) as doc:
        doc[0].insert_text(fitz.Point(bbox[0], bbox[1] + (bbox[3] - bbox[1]) * 0.8), "Ministry", fontsize=(bbox[3] - bbox[1]) * 0.8)
        doc.save(visible_source.with_suffix(".visible-tmp.pdf"))
    visible_source.with_suffix(".visible-tmp.pdf").replace(visible_source)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(visible_source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha
    result = compile_scanned_word_edits(visible_source, visible_output, envelope, resolved, source_sha)
    assert result == {"status": "REFUSED", "error_code": "VISIBLE_TEXT_LAYER_CONFLICT"}
    assert not visible_output.exists()


def test_searchable_scan_replacement_keeps_unsorted_copy_order_and_removes_stale_word(tmp_path):
    source = tmp_path / "searchable-source.pdf"
    output = tmp_path / "searchable-candidate.pdf"
    bbox = _fixture_pdf(source, hidden_ocr=True)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    with fitz.open(output) as edited:
        words = [word[4] for word in edited[0].get_text("words", sort=False)]
        assert words == ["hello", "of", "Education,", "Higher"]
        assert len(edited[0].search_for("hello")) == 1
        assert edited[0].search_for("Ministry") == []


def test_native_replacement_targets_repeated_word_by_position_and_preserves_neighbors(tmp_path):
    source = tmp_path / "native-repeated-source.pdf"
    output = tmp_path / "native-repeated-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry Ministry of Education")
    envelope, resolved, source_sha = _request(boxes[0], replacement="hello")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    assert result["target_results"][0]["representation"] == "native_text"
    with fitz.open(source) as original, fitz.open(output) as edited:
        original_words = original[0].get_text("words", sort=False)
        edited_words = edited[0].get_text("words", sort=False)
        assert [word[4] for word in edited_words] == ["hello", "Ministry", "of", "Education"]
        assert tuple(round(value, 2) for value in edited_words[2][:4]) == tuple(round(value, 2) for value in original_words[2][:4])
        original_drawings = original[0].get_drawings()
        edited_drawings = edited[0].get_drawings()
        assert len(original_drawings) == len(edited_drawings) == 1
        assert original_drawings[0]["items"] == edited_drawings[0]["items"]
        assert original_drawings[0]["color"] == edited_drawings[0]["color"]
        assert original_drawings[0]["width"] == edited_drawings[0]["width"]
    assert result["outside_changed_pixels_by_dpi"] == {"72": 0, "144": 0, "288": 0}


def test_native_later_repeated_word_is_refused_when_extraction_order_cannot_be_preserved(tmp_path):
    source = tmp_path / "native-later-source.pdf"
    output = tmp_path / "native-later-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry Ministry of Education")
    envelope, resolved, source_sha = _request(boxes[1], replacement="hello")
    resolved[0]["edit"]["word_id"] = "word-second"
    envelope["edits"][0]["word_id"] = "word-second"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "TEXT_READING_ORDER_UNSUPPORTED"}
    assert not output.exists()


def test_native_repeated_terminal_word_is_replaced_at_its_exact_position(tmp_path):
    source = tmp_path / "native-terminal-repeat-source.pdf"
    output = tmp_path / "native-terminal-repeat-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry of Ministry")
    envelope, resolved, source_sha = _request(boxes[2], replacement="hello")
    resolved[0]["edit"]["word_id"] = "word-last"
    envelope["edits"][0]["word_id"] = "word-last"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    with fitz.open(output) as edited:
        assert [word[4] for word in edited[0].get_text("words", sort=False)] == ["Ministry", "of", "hello"]
        assert edited[0].search_for("Ministry")
        assert len(edited[0].search_for("hello")) == 1


def test_native_deletion_updates_text_without_moving_neighbor_glyphs(tmp_path):
    source = tmp_path / "native-delete-source.pdf"
    output = tmp_path / "native-delete-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry of Education")
    envelope, resolved, source_sha = _request(boxes[0], replacement="")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    with fitz.open(source) as original, fitz.open(output) as edited:
        assert [word[4] for word in edited[0].get_text("words", sort=False)] == ["of", "Education"]
        assert tuple(round(value, 2) for value in edited[0].get_text("words", sort=False)[0][:4]) == tuple(
            round(value, 2) for value in original[0].get_text("words", sort=False)[1][:4]
        )


def test_native_replacement_can_grow_when_verified_free_space_is_available(tmp_path):
    source = tmp_path / "native-growth-source.pdf"
    output = tmp_path / "native-growth-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Hi")
    envelope, resolved, source_sha = _request(boxes[0], replacement="Greetings", expected="Hi")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    with fitz.open(output) as edited:
        assert [word[4] for word in edited[0].get_text("words", sort=False)] == ["Greetings"]


def test_mixed_page_routes_native_and_hidden_scan_words_by_target_representation(tmp_path):
    source = tmp_path / "mixed-source.pdf"
    output = tmp_path / "mixed-candidate.pdf"
    native_bbox, scan_bbox = _mixed_native_scan_pdf(source)
    edits = [
        {"page_num": 1, "element_id": "native-line", "word_id": "native-notice", "expected_original_text": "Notice",
         "replacement_text": "Report", "style_override": None, "fit_policy": "preserve_neighbors"},
        {"page_num": 1, "element_id": "scan-line", "word_id": "scan-ministry", "expected_original_text": "Ministry",
         "replacement_text": "hello", "style_override": None, "fit_policy": "preserve_neighbors"},
    ]
    digest = "sha256:" + "1" * 64
    envelope = {"edit_protocol": PROTOCOL, "source_tracker": "source-key", "extraction_job_id": "extract-1",
                "baseline_digest": digest, "edits": edits}
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved = [
        {"edit": edits[0], "bbox": native_bbox, "page_size": [595.0, 842.0], "source_sha256": source_sha, "baseline_digest": digest},
        {"edit": edits[1], "bbox": scan_bbox, "page_size": [595.0, 842.0], "source_sha256": source_sha, "baseline_digest": digest},
    ]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    assert [target["representation"] for target in result["target_results"]] == ["native_text", "searchable_scan"]
    with fitz.open(output) as edited:
        assert [word[4] for word in edited[0].get_text("words", sort=False)] == ["Report", "hello", "of", "Education,", "Higher"]
        assert len(edited[0].search_for("Ministry")) == 0


def test_mixed_page_refuses_all_operations_if_scan_target_is_stamped(tmp_path):
    source = tmp_path / "mixed-stamped-source.pdf"
    output = tmp_path / "mixed-stamped-candidate.pdf"
    native_bbox, scan_bbox = _mixed_native_scan_pdf(source, stamp=True)
    edits = [
        {"page_num": 1, "element_id": "native-line", "word_id": "native-notice", "expected_original_text": "Notice",
         "replacement_text": "Report", "style_override": None, "fit_policy": "preserve_neighbors"},
        {"page_num": 1, "element_id": "scan-line", "word_id": "scan-ministry", "expected_original_text": "Ministry",
         "replacement_text": "hello", "style_override": None, "fit_policy": "preserve_neighbors"},
    ]
    digest = "sha256:" + "1" * 64
    envelope = {"edit_protocol": PROTOCOL, "source_tracker": "source-key", "extraction_job_id": "extract-1",
                "baseline_digest": digest, "edits": edits}
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved = [
        {"edit": edits[0], "bbox": native_bbox, "page_size": [595.0, 842.0], "source_sha256": source_sha, "baseline_digest": digest},
        {"edit": edits[1], "bbox": scan_bbox, "page_size": [595.0, 842.0], "source_sha256": source_sha, "baseline_digest": digest},
    ]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "REFUSED"
    assert result["error_code"] == "BACKGROUND_COLORED_OVERLAP"
    assert not output.exists()


def test_native_custom_font_is_refused_without_creating_candidate(tmp_path):
    source = tmp_path / "native-custom-font-source.pdf"
    output = tmp_path / "native-custom-font-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry of Education", fontname="custom")
    envelope, resolved, source_sha = _request(boxes[0])
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "NATIVE_FONT_UNSUPPORTED"}
    assert not output.exists()


def test_native_ligature_is_refused_before_mutation(tmp_path):
    source = tmp_path / "native-ligature-source.pdf"
    output = tmp_path / "native-ligature-candidate.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="DejaVuFixture", fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    page.insert_text((60, 100), "\ufb01", fontname="DejaVuFixture", fontsize=14)
    bbox = [float(value) for value in page.get_text("words")[0][:4]]
    doc.save(source)
    doc.close()
    envelope, resolved, source_sha = _request(bbox, replacement="fi", expected="\ufb01")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "NATIVE_GLYPH_CLUSTER_UNSUPPORTED"}
    assert not output.exists()


def test_native_overprinted_word_is_refused_as_ambiguous(tmp_path):
    source = tmp_path / "native-overprint-source.pdf"
    output = tmp_path / "native-overprint-candidate.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "Ministry", fontname="helv", fontsize=14)
    bbox = [float(value) for value in page.get_text("words")[0][:4]]
    page.insert_text((60, 100), "Ministry", fontname="helv", fontsize=14)
    doc.save(source)
    doc.close()
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "NATIVE_TARGET_AMBIGUOUS"}
    assert not output.exists()


def test_native_replacement_that_collides_with_following_text_is_refused(tmp_path):
    source = tmp_path / "native-overflow-source.pdf"
    output = tmp_path / "native-overflow-candidate.pdf"
    boxes = _native_fixture_pdf(source, "Ministry of Education")
    envelope, resolved, source_sha = _request(boxes[0], replacement="ExtremelyLongReplacement")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "REPLACEMENT_DOES_NOT_FIT"}
    assert not output.exists()


def test_native_word_over_vector_line_is_refused_without_modifying_source_or_output(tmp_path):
    source = tmp_path / "native-vector-overlap-source.pdf"
    output = tmp_path / "native-vector-overlap-candidate.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "Ministry of Education", fontname="helv", fontsize=14)
    bbox = [float(value) for value in page.get_text("words")[0][:4]]
    page.draw_line((bbox[0], bbox[1] + 5), (bbox[2], bbox[1] + 5), color=(0.8, 0, 0), width=1)
    doc.save(source)
    doc.close()
    source_bytes = source.read_bytes()
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "NATIVE_GRAPHICS_OVERLAP"}
    assert not output.exists()
    assert source.read_bytes() == source_bytes


def test_native_text_over_scan_image_is_refused_as_layer_conflict(tmp_path):
    source = tmp_path / "native-over-scan-source.pdf"
    output = tmp_path / "native-over-scan-candidate.pdf"
    image = Image.new("RGB", (595, 842), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buffer.getvalue())
    page.insert_text((60, 100), "Ministry", fontname="helv", fontsize=14)
    bbox = [float(value) for value in page.get_text("words")[0][:4]]
    doc.save(source)
    doc.close()
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "VISIBLE_TEXT_LAYER_CONFLICT"}
    assert not output.exists()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"stamp": True}, "BACKGROUND_COLORED_OVERLAP"),
        ({"border": True}, "BACKGROUND_RULE_OVERLAP"),
        ({"rotation": 90}, "UNSUPPORTED_PAGE_GEOMETRY"),
        ({"crop": True}, "UNSUPPORTED_PAGE_GEOMETRY"),
    ],
)
def test_refuses_unsupported_background_or_geometry_without_output(tmp_path, kwargs, code):
    source = tmp_path / "source.pdf"
    output = tmp_path / "candidate.pdf"
    bbox = _fixture_pdf(source, **kwargs)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": code}
    assert not output.exists()


def test_refuses_unknown_protocol_and_all_or_nothing_target_identity(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "candidate.pdf"
    bbox = _fixture_pdf(source)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha
    envelope["edit_protocol"] = "platen_word_edits.v999"
    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)
    assert result["status"] == "REFUSED"
    assert result["error_code"] == "UNKNOWN_WORD_EDIT_PROTOCOL"
    assert not output.exists()

    envelope["edit_protocol"] = PROTOCOL
    second = dict(resolved[0])
    second["edit"] = dict(second["edit"], word_id="word-stale")
    envelope["edits"].append(second["edit"])
    resolved.append(second)
    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)
    assert result["status"] == "REFUSED"
    assert result["error_code"] == "WORD_EDIT_TARGET_COLLISION"
    assert not output.exists()


def test_refuses_non_english_text_and_overflow(tmp_path):
    source = tmp_path / "source.pdf"
    bbox = _fixture_pdf(source)
    for replacement, code in [("𠮷", "UNSUPPORTED_REPLACEMENT_TEXT"), ("replacement-too-wide-" * 20, "REPLACEMENT_DOES_NOT_FIT")]:
        output = tmp_path / ("candidate-" + str(len(replacement)) + ".pdf")
        envelope, resolved, source_sha = _request(bbox, replacement)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        resolved[0]["source_sha256"] = source_sha
        result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)
        assert result["status"] == "REFUSED"
        assert result["error_code"] == code
        assert not output.exists()


def test_compiles_lossy_mildly_skewed_scan_with_localized_patch(tmp_path):
    source = tmp_path / "lossy-skewed.pdf"
    output = tmp_path / "lossy-skewed-candidate.pdf"
    bbox = _fixture_pdf(source, lossy=True, skew=2.5)
    envelope, resolved, source_sha = _request(bbox)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved[0]["source_sha256"] = source_sha

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    assert result["outside_changed_pixels_144dpi"] == 0
    assert result["image_streams_preserved"] is True


def test_multiple_repeated_word_edits_are_all_or_nothing(tmp_path):
    clean_source = tmp_path / "two-clean-pages.pdf"
    clean_output = tmp_path / "two-clean-candidate.pdf"
    boxes = _two_page_fixture(clean_source, tmp_path)
    edits = [
        {"page_num": index + 1, "element_id": f"line-{index + 1}", "word_id": f"word-{index + 1}",
         "expected_original_text": "Ministry", "replacement_text": "hello", "style_override": None,
         "fit_policy": "preserve_neighbors"}
        for index in range(2)
    ]
    baseline_digest = "sha256:" + "1" * 64
    envelope = {"edit_protocol": PROTOCOL, "source_tracker": "source-key", "extraction_job_id": "extract-1",
                "baseline_digest": baseline_digest, "edits": edits}
    source_sha = hashlib.sha256(clean_source.read_bytes()).hexdigest()
    resolved = [
        {"edit": edit, "bbox": box, "page_size": [595.0, 842.0], "source_sha256": source_sha,
         "baseline_digest": baseline_digest}
        for edit, box in zip(edits, boxes, strict=True)
    ]
    success = compile_scanned_word_edits(clean_source, clean_output, envelope, resolved, source_sha)
    assert success["status"] == "EDITED", success
    with fitz.open(clean_output) as candidate:
        assert len(candidate) == 2
        assert [page.get_text().count("hello") for page in candidate] == [1, 1]

    stamped_source = tmp_path / "second-page-stamped.pdf"
    refused_output = tmp_path / "refused-two-page-candidate.pdf"
    stamped_boxes = _two_page_fixture(stamped_source, tmp_path, second_stamp=True)
    stamped_sha = hashlib.sha256(stamped_source.read_bytes()).hexdigest()
    stamped_resolved = [
        {"edit": edit, "bbox": box, "page_size": [595.0, 842.0], "source_sha256": stamped_sha,
         "baseline_digest": baseline_digest}
        for edit, box in zip(edits, stamped_boxes, strict=True)
    ]
    refusal = compile_scanned_word_edits(stamped_source, refused_output, envelope, stamped_resolved, stamped_sha)
    assert refusal == {"status": "REFUSED", "error_code": "BACKGROUND_COLORED_OVERLAP"}
    assert not refused_output.exists()

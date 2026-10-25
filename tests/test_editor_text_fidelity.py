"""F-16 export regressions: real native PDFs, exact text and visual geometry."""

import json
import random

import pymupdf as fitz
import pytest
from PIL import Image, ImageChops, ImageStat

from app.api.tools.editor.document import (
    compile_document,
    compute_effective_style,
    compute_text_diff,
    resolve_surgical_targets,
)
from app.core.editor_ocr_engine import execute_editor_ocr


BODY = "Untouched body content."
HEADING = "PLATEN AUDIT SYNTHETIC ALPHA"


def source_pdf(path, text):
    with fitz.open() as document:
        page = document.new_page(width=600, height=220)
        page.insert_text((40, 60), text, fontsize=16, fontname="helv")
        page.insert_text((40, 130), BODY, fontsize=11, fontname="helv")
        document.save(path)


def image(page, clip):
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def heading_chars(page):
    return sorted([
        {**char, "size": span["size"], "font": span["font"]}
        for block in page.get_text("rawdict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        for char in span["chars"]
        if abs(char["origin"][1] - 60) < 0.1
    ], key=lambda char: char["origin"][0])


def compile_edit(tmp_path, monkeypatch, original, edited, *, engine="internal", style=None):
    source, output, layout_path = [tmp_path / name for name in ("source.pdf", "output.pdf", "layout.json")]
    source_pdf(source, original)
    monkeypatch.setenv("EDITOR_OCR_ENGINE", engine)
    layout = execute_editor_ocr(source, language_mode="EXPLICIT", languages=["eng"])
    element = layout["pages"][0]["elements"][0]
    element["text"] = edited
    if style is not None:
        element["style"] = style
    layout_path.write_text(json.dumps(layout))
    compile_document(str(source), str(output), str(layout_path))
    return source, output, element


@pytest.mark.parametrize("engine", ["internal", "sdk"])
def test_audit_append_preserves_space_native_size_and_rendered_geometry(tmp_path, monkeypatch, engine):
    edited = HEADING + " EDITED"
    source, output, element = compile_edit(tmp_path, monkeypatch, HEADING, edited, engine=engine)
    reference = tmp_path / "reference.pdf"
    source_pdf(reference, edited)
    with fitz.open(source) as before, fitz.open(output) as after, fitz.open(reference) as ideal:
        chars = heading_chars(after[0])
        assert "".join(char["c"] for char in chars) == edited
        assert after[0].get_text("text", sort=True).splitlines()[0] == edited
        assert [char["size"] for char in chars] == pytest.approx([16] * len(edited), abs=0.01)
        expected_chars = heading_chars(ideal[0])
        for actual, expected in zip(chars, expected_chars):
            assert actual["origin"] == pytest.approx(expected["origin"], abs=0.01)
            assert actual["bbox"] == pytest.approx(expected["bbox"], abs=0.01)
        heading_clip = fitz.Rect(30, 30, 550, 90)
        visual_delta = ImageChops.difference(image(after[0], heading_clip), image(ideal[0], heading_clip))
        assert max(ImageStat.Stat(visual_delta).mean) < 0.25
        body_clip = fitz.Rect(30, 110, 400, 150)
        assert image(before[0], body_clip).tobytes() == image(after[0], body_clip).tobytes()
        assert after[0].get_text().count(BODY) == 1
        # The input's estimated OCR-line size is not a user font override.
        assert element.get("style") is None


@pytest.mark.parametrize(("original", "edited"), [
    ("ALPHA", "ALPHAEDITED"),
    ("ALPHA GAMMA", "ALPHA BETA GAMMA"),
    ("ALPHA BETA GAMMA", "ALPHA ZETA GAMMA"),
    ("ALPHA BETA GAMMA", "ALPHA GAMMA"),
    ("ALPHA BETA", "ALPHA  BETA"),
    ("ALPHA", "NEW ALPHA"),
])
def test_text_edits_preserve_words_spaces_and_baselines(tmp_path, monkeypatch, original, edited):
    source, output, _ = compile_edit(tmp_path, monkeypatch, original, edited)
    with fitz.open(source) as before, fitz.open(output) as after:
        chars = heading_chars(after[0])
        assert "".join(char["c"] for char in chars) == edited
        assert [char["size"] for char in chars] == pytest.approx([16] * len(chars), abs=0.01)
        assert all(char["origin"][1] == pytest.approx(60, abs=0.01) for char in chars)
        assert all(left["bbox"][2] <= right["bbox"][0] + 0.01 for left, right in zip(chars, chars[1:]))
        assert after[0].get_text().count(BODY) == 1
        assert image(before[0], fitz.Rect(30, 110, 400, 150)).tobytes() == image(after[0], fitz.Rect(30, 110, 400, 150)).tobytes()


@pytest.mark.parametrize("edited", [HEADING, HEADING + " EDITED"])
def test_explicit_font_size_and_style_remain_authoritative(tmp_path, monkeypatch, edited):
    _, output, _ = compile_edit(tmp_path, monkeypatch, HEADING, edited, style={"fontSize": 20, "bold": True})
    with fitz.open(output) as document:
        chars = heading_chars(document[0])
        assert "".join(char["c"] for char in chars) == edited
        selected = chars if edited == HEADING else chars[-len("EDITED"):]
        assert all(char["size"] == pytest.approx(20) for char in selected)
        assert all(char["font"] == "Helvetica-Bold" for char in selected)


def test_native_font_size_beats_projection_estimate_but_not_explicit_override():
    element = {"size": 25.3}
    original = {"name": "Helvetica", "size": 16}
    assert compute_effective_style(element, original)["font_size"] == 16
    assert compute_effective_style({**element, "style": {"fontSize": 20}}, original)["font_size"] == 20
    assert compute_effective_style(element)["font_size"] == 25.3  # OCR/fallback sizing is unchanged.


@pytest.mark.parametrize(("original", "edited"), [
    (HEADING, HEADING + " EDITED"),
    ("ALPHA", "ALPHAEDITED"),
    ("Hello world", "Hello wonderful world"),
    ("Hello wonderful world", "Hello world"),
    ("ALPHA BETA", "ALPHA  BETA"),
    ("Developer", "Senior Developer"),
    ("First old middle last", "First new middle last extra"),
    ("", " NEW "),
    (" OLD ", ""),
    (" ", "  "),
    (" ALPHA BETA", "ALPHA BETA EXTRA"),
    ("ALPHA BETA ", "NEW ALPHA BETA"),
])
def test_diff_reconstructs_exact_edited_text(original, edited):
    operations = compute_text_diff(original, edited)
    result = original
    for operation in sorted(operations, key=lambda op: op["original_start"], reverse=True):
        start, end = operation["original_start"], operation["original_end"]
        assert 0 <= start <= end <= len(original)
        assert original[start:end] == operation["original_substring"]
        result = result[:start] + operation["replacement_substring"] + result[end:]
    assert result == edited


def test_interior_insertion_targets_real_suffix_not_neighboring_element(tmp_path):
    source = tmp_path / "source.pdf"
    source_pdf(source, "ALPHA GAMMA")
    with fitz.open(source) as document:
        span = document[0].get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
        rect = fitz.Rect(span["bbox"])
        element = {"original_text": "ALPHA GAMMA", "text": "ALPHA BETA GAMMA", "x": rect.x0,
                   "y": rect.y0, "width": rect.width, "height": rect.height, "size": 25.3}
        targets = resolve_surgical_targets(document[0], element)
        assert len(targets) == 1
        assert targets[0]["operation"] == "replace"
        assert targets[0]["original_substring"] == "GAMMA"
        assert targets[0]["replacement_substring"] == "BETA GAMMA"
        assert targets[0]["target_bbox"][0] > rect.x0


def test_repeated_words_and_whitespace_have_lossless_diff_ranges():
    random_source = random.Random(16)

    def synthetic_text():
        words = [random_source.choice(["ALPHA", "BETA", "GAMMA"]) for _ in range(random_source.randrange(5))]
        return random_source.choice(["", " "]) + random_source.choice([" ", "  "]).join(words) + random_source.choice(["", " "])

    for _ in range(2000):
        original, edited = synthetic_text(), synthetic_text()
        result = original
        for operation in sorted(compute_text_diff(original, edited), key=lambda op: op["original_start"], reverse=True):
            start, end = operation["original_start"], operation["original_end"]
            assert 0 <= start <= end <= len(original)
            assert original[start:end] == operation["original_substring"]
            result = result[:start] + operation["replacement_substring"] + result[end:]
        assert result == edited


def test_reflow_does_not_redact_other_elements_on_the_same_baseline(tmp_path):
    source, output, layout_path = [tmp_path / name for name in ("source.pdf", "output.pdf", "layout.json")]
    with fitz.open() as document:
        page = document.new_page(width=600, height=220)
        page.insert_text((20, 60), "LEFT", fontsize=16)
        page.insert_text((130, 60), "ALPHA GAMMA", fontsize=16)
        page.insert_text((450, 60), "RIGHT", fontsize=16)
        span = next(span for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
                    for span in line["spans"] if span["text"] == "ALPHA GAMMA")
        rect = fitz.Rect(span["bbox"])
        element = {"original_text": "ALPHA GAMMA", "text": "ALPHA BETA GAMMA", "x": rect.x0,
                   "y": rect.y0, "width": rect.width, "height": rect.height, "size": 25.3}
        document.save(source)
    layout_path.write_text(json.dumps({"pages": [{"elements": [element]}]}))
    compile_document(str(source), str(output), str(layout_path))
    with fitz.open(source) as before, fitz.open(output) as after:
        assert [word[4] for word in after[0].get_text("words", sort=True)] == ["LEFT", "ALPHA", "BETA", "GAMMA", "RIGHT"]
        for clip in [fitz.Rect(10, 30, 100, 90), fitz.Rect(430, 30, 550, 90)]:
            assert image(before[0], clip).tobytes() == image(after[0], clip).tobytes()

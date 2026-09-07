from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pymupdf as fitz
import pytest

from app.api.tools.editor.document import compile_document, resolve_surgical_targets
from app.core.editor_ocr_engine import execute_editor_ocr
from app.core.studio_editor_extraction_engine import execute_studio_editor_extraction


ROTATIONS = (0, 90, 180, 270)


def _rotated_native_pdf(path: Path, rotation: int) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((70, 120), "Keep ReplaceMe", fontsize=24, fontname="helv")
    # Exercise the visible CropBox contract while keeping the source text in
    # deterministic, unrotated PDF user-unit coordinates.
    page.set_cropbox(fitz.Rect(20, 30, 520, 730))
    page.set_rotation(rotation)
    document.save(path)
    document.close()


def _editor_layout(path: Path, engine: str, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("EDITOR_OCR_ENGINE", engine)
    return execute_editor_ocr(path, language_mode="EXPLICIT", languages=["eng"])


def _word_material(page: dict) -> list[tuple[str, float, float, float, float]]:
    return [
        (
            word["text"],
            float(word["x"]),
            float(word["y"]),
            float(word["width"]),
            float(word["height"]),
        )
        for element in page["elements"]
        for word in element["word_geometry"]
    ]


def _source_word_material(path: Path) -> list[tuple[str, float, float, float, float]]:
    with fitz.open(path) as document:
        return [
            (
                str(word[4]),
                float(word[0]),
                float(word[1]),
                float(word[2] - word[0]),
                float(word[3] - word[1]),
            )
            for word in document[0].get_text("words")
        ]


def _assert_word_material(
    actual: list[tuple[str, float, float, float, float]],
    expected: list[tuple[str, float, float, float, float]],
) -> None:
    assert [item[0] for item in actual] == [item[0] for item in expected]
    for actual_word, expected_word in zip(actual, expected):
        assert actual_word[1:] == pytest.approx(expected_word[1:], abs=0.01)


@pytest.mark.parametrize("engine", ["internal", "sdk"])
@pytest.mark.parametrize("rotation", ROTATIONS)
def test_general_editor_rotation_rollback_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    rotation: int,
) -> None:
    source = tmp_path / f"source-{rotation}.pdf"
    output = tmp_path / f"compiled-{engine}-{rotation}.pdf"
    layout_path = tmp_path / f"layout-{engine}-{rotation}.json"
    _rotated_native_pdf(source, rotation)

    layout = _editor_layout(source, engine, monkeypatch)
    sdk_layout = _editor_layout(source, "sdk", monkeypatch)
    page = layout["pages"][0]
    sdk_page = sdk_layout["pages"][0]
    expected_words = _source_word_material(source)
    with fitz.open(source) as document:
        assert page["width"] == pytest.approx(document[0].rect.width, abs=0.01)
        assert page["height"] == pytest.approx(document[0].rect.height, abs=0.01)
    assert page["width"] == pytest.approx(sdk_page["width"], abs=0.01)
    assert page["height"] == pytest.approx(sdk_page["height"], abs=0.01)

    # Extraction, element projection, counts, and canonical word geometry.
    assert page["kind"] == "text"
    assert page["word_count"] == 2
    assert page["text_block_count"] == 1
    assert len(page["elements"]) == 1
    assert page["elements"][0]["text"] == "Keep ReplaceMe"
    assert "Keep ReplaceMe" in page["elements"][0]["original_text"]
    _assert_word_material(_word_material(page), expected_words)
    _assert_word_material(_word_material(page), _word_material(sdk_page))

    # A 90-degree regression must not turn (x, y, w, h) into
    # (bbox * page.rotation_matrix), which is the exact 0578d590 failure.
    if rotation == 90:
        assert page["elements"][0]["word_geometry"][0]["x"] == pytest.approx(
            expected_words[0][1], abs=0.01
        )

    # The compiler consumes the same canonical boxes. Resolve the edited word
    # before compilation so a permissive text-map fallback cannot hide a bad
    # editor projection.
    edited_layout = json.loads(json.dumps(layout))
    edited_element = edited_layout["pages"][0]["elements"][0]
    edited_element["text"] = "Keep Replaced"
    with fitz.open(source) as document:
        targets = resolve_surgical_targets(document[0], edited_element)
    replacement_targets = [target for target in targets if target["original_substring"] == "ReplaceMe"]
    assert len(replacement_targets) == 1
    target_bbox = replacement_targets[0]["target_bbox"]
    replace_word = next(word for word in expected_words if word[0] == "ReplaceMe")
    assert target_bbox == pytest.approx(
        [replace_word[1], replace_word[2], replace_word[1] + replace_word[3], replace_word[2] + replace_word[4]],
        abs=0.01,
    )

    layout_path.write_text(json.dumps(edited_layout), encoding="utf-8")
    compile_document(str(source), str(output), str(layout_path))

    with fitz.open(output) as compiled:
        assert compiled.page_count == 1
        compiled_text = compiled[0].get_text("text")
        assert "Keep" in compiled_text
        assert "Replaced" in compiled_text
        assert "ReplaceMe" not in compiled_text

    qpdf = shutil.which("qpdf")
    if qpdf is None:
        pytest.skip("qpdf is required for the PDF structural check")
    subprocess.run([qpdf, "--check", str(output)], check=True, capture_output=True, text=True)


def test_studio_internal_keeps_legacy_visible_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "studio-rotation-90.pdf"
    _rotated_native_pdf(source, 90)

    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "internal")
    studio_layout = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])
    general_layout = _editor_layout(source, "internal", monkeypatch)
    sdk_layout = _editor_layout(source, "sdk", monkeypatch)

    with fitz.open(source) as document:
        page = document[0]
        legacy_visible_words = [
            (
                str(word[4]),
                float((fitz.Rect(word[:4]) * page.rotation_matrix).x0),
                float((fitz.Rect(word[:4]) * page.rotation_matrix).y0),
                float((fitz.Rect(word[:4]) * page.rotation_matrix).width),
                float((fitz.Rect(word[:4]) * page.rotation_matrix).height),
            )
            for word in page.get_text("words")
        ]

    _assert_word_material(_word_material(studio_layout["pages"][0]), legacy_visible_words)
    _assert_word_material(_word_material(general_layout["pages"][0]), _word_material(sdk_layout["pages"][0]))
    assert _word_material(studio_layout["pages"][0]) != _word_material(general_layout["pages"][0])

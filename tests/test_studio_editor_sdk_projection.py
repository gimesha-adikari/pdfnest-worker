from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont
from platen_document import DocumentProcessor, OCRProfile

from app.core.editor_ocr_projection import project_studio_editor_result
from app.core.studio_editor_extraction_engine import execute_studio_editor_extraction


ROTATIONS = (0, 90, 180, 270)


def _native_pdf(path: Path, rotation: int, *, cropbox: bool = False) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    if cropbox:
        page.set_cropbox(fitz.Rect(20, 30, 520, 730))
    page.insert_text((50, 62), "Sample", fontsize=20, fontname="helv")
    page.insert_text((50, 122), "This", fontsize=12, fontname="helv")
    page.set_rotation(rotation)
    document.save(path)
    document.close()


def _mixed_pdf(path: Path) -> None:
    image = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    draw.text((70, 120), "Scanned Studio Page", font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")

    document = fitz.open()
    native = document.new_page(width=595, height=842)
    native.set_cropbox(fitz.Rect(0, 0, 595, 842))
    native.insert_text((50, 62), "Sample", fontsize=20, fontname="helv")
    native.set_rotation(90)
    document.new_page(width=432, height=240).insert_image(
        fitz.Rect(0, 0, 432, 240),
        stream=stream.getvalue(),
    )
    document.save(path)
    document.close()


def _word_material(layout: dict[str, object]) -> list[tuple[str, float, float, float, float]]:
    return [
        (
            word["text"],
            float(word["x"]),
            float(word["y"]),
            float(word["width"]),
            float(word["height"]),
        )
        for page in layout["pages"]
        for element in page["elements"]
        for word in element["word_geometry"]
    ]


def _element_material(layout: dict[str, object]) -> list[tuple[str, str, float, float, float, float]]:
    return [
        (
            element["text"],
            element["source"],
            float(element["x"]),
            float(element["y"]),
            float(element["width"]),
            float(element["height"]),
        )
        for page in layout["pages"]
        for element in page["elements"]
    ]


def _assert_word_material_equal(actual: list[tuple[str, float, float, float, float]], expected: list[tuple[str, float, float, float, float]]) -> None:
    assert [item[0] for item in actual] == [item[0] for item in expected]
    for actual_word, expected_word in zip(actual, expected):
        assert actual_word[1:] == pytest.approx(expected_word[1:], abs=0.01)


def _assert_element_material_equal(actual: list[tuple[str, str, float, float, float, float]], expected: list[tuple[str, str, float, float, float, float]]) -> None:
    assert [(item[0], item[1]) for item in actual] == [(item[0], item[1]) for item in expected]
    for actual_element, expected_element in zip(actual, expected):
        assert actual_element[2:] == pytest.approx(expected_element[2:], abs=0.01)


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_studio_sdk_projection_matches_internal_visible_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rotation: int,
) -> None:
    source = tmp_path / f"native-{rotation}.pdf"
    _native_pdf(source, rotation)

    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "internal")
    internal = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "sdk")
    sdk = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])

    internal_page = internal["pages"][0]
    sdk_page = sdk["pages"][0]
    assert (sdk_page["width"], sdk_page["height"]) == pytest.approx(
        (595, 842) if rotation in (0, 180) else (842, 595),
        abs=0.01,
    )
    assert (sdk_page["width"], sdk_page["height"]) == pytest.approx(
        (internal_page["width"], internal_page["height"]),
        abs=0.01,
    )
    assert sdk_page["source"] == internal_page["source"] == "NATIVE_EXTRACTION"
    assert sdk_page["kind"] == internal_page["kind"] == "text"
    assert sdk_page["provenance"] == internal_page["provenance"] == ["pymupdf_native_extractor"]
    assert sdk_page["capabilities"] == internal_page["capabilities"]
    assert sdk_page["word_count"] == internal_page["word_count"] == 2
    assert sdk_page["text_block_count"] == internal_page["text_block_count"] == 2
    assert len(sdk_page["elements"]) == len(internal_page["elements"]) == 2
    assert [element["text"] for element in sdk_page["elements"]] == [
        element["text"] for element in internal_page["elements"]
    ]
    assert [element["font"] for element in sdk_page["elements"]] == [
        element["font"] for element in internal_page["elements"]
    ]
    assert [element["reading_order"] for element in sdk_page["elements"]] == [
        element["reading_order"] for element in internal_page["elements"]
    ]
    assert sdk["language_mode"] == internal["language_mode"] == "EXPLICIT"
    assert sdk["languages"] == internal["languages"] == ["eng"]
    _assert_word_material_equal(_word_material(sdk), _word_material(internal))
    _assert_element_material_equal(_element_material(sdk), _element_material(internal))


def test_studio_sdk_projection_reproduces_production_270_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "native-270.pdf"
    _native_pdf(source, 270)
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "sdk")

    raw = DocumentProcessor().extract_text(
        source,
        language="eng",
        language_mode="EXPLICIT",
        languages=("eng",),
        profile=OCRProfile.OCR_TEXT_V2,
    )
    raw_sample = next(token for token in raw.pages[0].tokens if token.text == "Sample")
    assert (raw_sample.bbox.x, raw_sample.bbox.y, raw_sample.bbox.width, raw_sample.bbox.height) == pytest.approx(
        (50.0, 40.5, 67.8, 27.48),
        abs=0.01,
    )

    layout = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])
    words = {
        word["text"]: word
        for page in layout["pages"]
        for element in page["elements"]
        for word in element["word_geometry"]
    }

    assert (words["Sample"]["x"], words["Sample"]["y"], words["Sample"]["width"], words["Sample"]["height"]) == pytest.approx(
        (40.5, 477.2, 27.48, 67.8),
        abs=0.01,
    )
    assert (words["This"]["x"], words["This"]["y"], words["This"]["width"], words["This"]["height"]) == pytest.approx(
        (109.1, 522.332, 16.488, 22.668),
        abs=0.01,
    )


@pytest.mark.parametrize("rotation", (90, 270))
def test_studio_sdk_projection_preserves_nonzero_cropbox_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rotation: int,
) -> None:
    source = tmp_path / f"cropbox-{rotation}.pdf"
    _native_pdf(source, rotation, cropbox=True)
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "internal")
    internal = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "sdk")
    sdk = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])

    _assert_word_material_equal(_word_material(sdk), _word_material(internal))
    _assert_element_material_equal(_element_material(sdk), _element_material(internal))


def test_studio_sdk_projection_leaves_ocr_geometry_visible() -> None:
    word = SimpleNamespace(
        id="ocr-word",
        text="Scanned",
        bbox=SimpleNamespace(x=12.0, y=24.0, width=80.0, height=18.0),
        confidence=SimpleNamespace(raw_value=0.9),
    )
    line = SimpleNamespace(
        text="Scanned",
        bbox=SimpleNamespace(x=12.0, y=24.0, width=80.0, height=18.0),
        token_ids=("ocr-word",),
    )
    page = SimpleNamespace(
        page_index=0,
        geometry=SimpleNamespace(width=842.0, height=595.0, rotation=270),
        content_classification="IMAGE_SCAN",
        processing_source="OCR_RECOGNITION",
        tokens=(word,),
        tokens_by_id={"ocr-word": word},
        lines=(line,),
        reading_order=("ocr-word",),
        provenance_refs=("tesseract_v2",),
        capabilities=frozenset({"TEXT", "WORD_GEOMETRY"}),
    )
    result = SimpleNamespace(
        pages=(page,),
        source=SimpleNamespace(to_dict=lambda: {"page_count": 1, "filename": "scan.pdf"}),
    )

    projected = project_studio_editor_result(result)
    projected_word = projected["pages"][0]["elements"][0]["word_geometry"][0]
    assert (projected_word["x"], projected_word["y"], projected_word["width"], projected_word["height"]) == (
        12.0,
        24.0,
        80.0,
        18.0,
    )


def test_studio_sdk_projection_is_per_page_for_mixed_rotated_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed.pdf"
    _mixed_pdf(source)
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "internal")
    internal = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "sdk")
    sdk = execute_studio_editor_extraction(source, language_mode="EXPLICIT", languages=["eng"])

    assert len(sdk["pages"]) == len(internal["pages"]) == 2
    assert sdk["pages"][0]["source"] == internal["pages"][0]["source"] == "NATIVE_EXTRACTION"
    assert sdk["pages"][1]["source"] == internal["pages"][1]["source"] == "OCR_RECOGNITION"
    _assert_word_material_equal(_word_material(sdk), _word_material(internal))
    _assert_element_material_equal(_element_material(sdk), _element_material(internal))

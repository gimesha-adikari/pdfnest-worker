from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.core.studio_markup_region_ocr_engine import (
    STUDIO_MARKUP_REGION_OCR_ENGINE_ENV,
    execute_studio_markup_region_ocr,
)


def _native_pdf(path: Path, *, rotation: int = 0, cropbox: bool = False) -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((35, 70), "Alpha Bravo", fontsize=24)
    if cropbox:
        page.set_cropbox(fitz.Rect(20, 20, 280, 180))
    if rotation:
        page.set_rotation(rotation)
    document.save(path)
    document.close()


def _scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    draw.text((70, 120), "Studio Region Alpha Bravo", font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    document = fitz.open()
    document.new_page(width=450, height=180).insert_image(fitz.Rect(0, 0, 450, 180), stream=stream.getvalue())
    document.save(path)
    document.close()


def _mixed_pdf(path: Path) -> None:
    native = path.with_name("native-source.pdf")
    scanned = path.with_name("scanned-source.pdf")
    _native_pdf(native)
    _scanned_pdf(scanned)
    document = fitz.open()
    with fitz.open(native) as native_document:
        document.insert_pdf(native_document)
    with fitz.open(scanned) as scanned_document:
        document.insert_pdf(scanned_document)
    document.save(path)
    document.close()


def _box(*, color: str = "#1A334C", width: float = 200, height: float = 70) -> list[dict[str, object]]:
    return [{"id": "region-1", "page": 1, "x": 15, "y": 25, "width": width, "height": height, "color": color}]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    selected_engine: str,
    source: Path,
    output: Path,
    *,
    action: str,
    mode: str,
    boxes: list[dict[str, object]],
) -> dict[str, Any]:
    monkeypatch.setenv(STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, selected_engine)
    return execute_studio_markup_region_ocr(source, output, boxes=boxes, action=action, mode=mode)


def _selection_material(result: dict[str, Any]) -> list[dict[str, object]]:
    return [
        {
            "page_index": item["page_index"],
            "matched_text": item["matched_text"],
            "word_ids": item["word_ids"],
            "reading_order_start": item["reading_order_start"],
            "reading_order_end": item["reading_order_end"],
            "group_rects": item["group_rects"],
            "source_type": item["source_type"],
            "confidence": item["confidence"],
            "provenance": item["provenance"],
        }
        for item in result["selections"]
    ]


def _annotations(path: Path) -> list[dict[str, object]]:
    document = fitz.open(path)
    values = [
        {
            "page": page_index + 1,
            "type": annotation.type[1],
            "rect": tuple(round(value, 3) for value in annotation.rect),
            "color": tuple(round(value, 4) for value in annotation.colors["stroke"]),
        }
        for page_index, page in enumerate(document)
        for annotation in (page.annots() or ())
    ]
    document.close()
    return values


def _drawing_colors(path: Path) -> list[tuple[float, float, float]]:
    document = fitz.open(path)
    values = []
    for drawing in document[0].get_drawings():
        color = drawing["fill"] or drawing["color"]
        if color is not None:
            values.append(tuple(round(value, 4) for value in color))
    document.close()
    return values


@pytest.mark.parametrize(
    ("action", "annotation_type"),
    [
        ("highlight", "Highlight"),
        ("underline", "Underline"),
        ("strikeout", "StrikeOut"),
    ],
)
def test_native_smart_region_parity_and_annotation_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    annotation_type: str,
) -> None:
    source = tmp_path / "native.pdf"
    _native_pdf(source)
    boxes = _box()
    internal = _run(monkeypatch, "internal", source, tmp_path / f"internal-{action}.pdf", action=action, mode="smart", boxes=boxes)
    sdk_output = tmp_path / f"sdk-{action}.pdf"
    sdk = _run(monkeypatch, "sdk", source, sdk_output, action=action, mode="smart", boxes=boxes)

    assert _selection_material(sdk) == _selection_material(internal)
    assert sdk["annotation_count"] == 1
    annotations = _annotations(sdk_output)
    assert len(annotations) == 1
    assert annotations[0]["type"] == annotation_type
    assert annotations[0]["color"] == pytest.approx((26 / 255, 51 / 255, 76 / 255), abs=0.001)


@pytest.mark.parametrize("mode", ["smart", "ocr"])
def test_scanned_region_parity_uses_one_sdk_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    source = tmp_path / "scanned.pdf"
    _scanned_pdf(source)
    boxes = _box(width=420, height=140)
    internal = _run(monkeypatch, "internal", source, tmp_path / f"internal-{mode}.pdf", action="highlight", mode=mode, boxes=boxes)
    sdk_output = tmp_path / f"sdk-{mode}.pdf"
    sdk = _run(monkeypatch, "sdk", source, sdk_output, action="highlight", mode=mode, boxes=boxes)

    assert _selection_material(sdk) == _selection_material(internal)
    assert sdk["extraction_performed"] is True
    assert sdk["document_result_reused"] is False
    assert sdk["annotation_count"] == len(sdk["selections"])
    assert [annotation["type"] for annotation in _annotations(sdk_output)] == ["Highlight"]
    document = fitz.open(sdk_output)
    assert document[0].get_images(full=True)
    document.close()


def test_mixed_document_switches_between_native_and_ocr_without_duplicate_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    _mixed_pdf(source)
    boxes = [
        {"id": "native", "page": 1, "x": 15, "y": 25, "width": 220, "height": 80, "color": "#FF0000"},
        {"id": "scanned", "page": 2, "x": 15, "y": 25, "width": 420, "height": 140, "color": "#0000FF"},
    ]
    internal_output = tmp_path / "mixed-internal.pdf"
    internal = _run(monkeypatch, "internal", source, internal_output, action="highlight", mode="smart", boxes=boxes)
    sdk = _run(monkeypatch, "sdk", source, tmp_path / "mixed-sdk.pdf", action="highlight", mode="smart", boxes=boxes)

    assert _selection_material(sdk) == _selection_material(internal)
    assert [selection["source_type"] for selection in sdk["selections"]] == ["native", "ocr"]
    assert len({word_id for selection in sdk["selections"] for word_id in selection["word_ids"]}) == sum(
        len(selection["word_ids"]) for selection in sdk["selections"]
    )
    assert [annotation["color"] for annotation in _annotations(internal_output)] == pytest.approx(
        [(1, 0, 0), (0, 0, 1)],
        abs=0.001,
    )


def test_sdk_manual_regions_skip_extraction_and_write_a_real_annotation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manual.pdf"
    _native_pdf(source)
    output = tmp_path / "manual-sdk.pdf"
    result = _run(monkeypatch, "sdk", source, output, action="underline", mode="manual", boxes=_box(color="#008000"))

    assert result["extraction_performed"] is False
    assert result["selection_count"] == 0
    annotations = _annotations(output)
    assert len(annotations) == 1
    assert annotations[0]["type"] == "Underline"
    assert annotations[0]["color"] == pytest.approx((0, 128 / 255, 0), abs=0.001)


@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
def test_multiple_overlapping_and_empty_regions_preserve_material_selection_and_colors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    source = tmp_path / "multiple.pdf"
    _native_pdf(source)
    boxes = [
        {"id": "first", "page": 1, "x": 20, "y": 40, "width": 75, "height": 55, "color": "#FF0000"},
        {"id": "overlap", "page": 1, "x": 20, "y": 40, "width": 175, "height": 55, "color": "#0000FF"},
        {"id": "empty", "page": 1, "x": 20, "y": 40, "width": 0, "height": 20, "color": "#00FF00"},
        {"id": "off-page", "page": 2, "x": 20, "y": 40, "width": 40, "height": 20, "color": "#00FF00"},
    ]
    internal_output = tmp_path / "multiple-internal.pdf"
    internal = _run(monkeypatch, "internal", source, internal_output, action=action, mode="smart", boxes=boxes)
    sdk_output = tmp_path / "multiple-sdk.pdf"
    sdk = _run(monkeypatch, "sdk", source, sdk_output, action=action, mode="smart", boxes=boxes)

    assert _selection_material(sdk) == _selection_material(internal)
    assert [selection["region_id"] for selection in internal["selections"]] == ["first", "overlap"]
    assert [region["status"] for region in sdk["regions"]] == ["annotated", "annotated", "empty", "out_of_bounds"]
    assert sdk["annotation_count"] == 2
    assert [annotation["color"] for annotation in _annotations(sdk_output)] == pytest.approx(
        [(1, 0, 0), (0, 0, 1)],
        abs=0.001,
    )
    assert [annotation["color"] for annotation in _annotations(internal_output)] == pytest.approx(
        [(1, 0, 0), (0, 0, 1)],
        abs=0.001,
    )


@pytest.mark.parametrize(
    ("action", "annotation_type"),
    [
        ("highlight", "Highlight"),
        ("underline", "Underline"),
        ("strikeout", "StrikeOut"),
    ],
)
@pytest.mark.parametrize("mode", ["smart", "ocr"])
def test_same_page_per_region_colors_match_sdk_for_all_actions_and_ocr_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    annotation_type: str,
    mode: str,
) -> None:
    source = tmp_path / f"{mode}.pdf"
    if mode == "smart":
        _native_pdf(source)
        boxes = [
            {"id": "region-red", "page": 1, "x": 25, "y": 35, "width": 75, "height": 55, "color": "#FF0000"},
            {"id": "region-blue", "page": 1, "x": 105, "y": 35, "width": 90, "height": 55, "color": "#0000FF"},
        ]
    else:
        _scanned_pdf(source)
        boxes = [
            {"id": "region-red", "page": 1, "x": 20, "y": 45, "width": 135, "height": 100, "color": "#FF0000"},
            {"id": "region-blue", "page": 1, "x": 150, "y": 45, "width": 150, "height": 100, "color": "#0000FF"},
        ]

    internal_output = tmp_path / f"internal-{mode}-{action}.pdf"
    sdk_output = tmp_path / f"sdk-{mode}-{action}.pdf"
    internal = _run(monkeypatch, "internal", source, internal_output, action=action, mode=mode, boxes=boxes)
    sdk = _run(monkeypatch, "sdk", source, sdk_output, action=action, mode=mode, boxes=boxes)

    assert _selection_material(internal) == _selection_material(sdk)
    assert [selection["region_id"] for selection in internal["selections"]] == ["region-red", "region-blue"]
    expected_colors = [(1, 0, 0), (0, 0, 1)]
    for output in (internal_output, sdk_output):
        annotations = _annotations(output)
        assert [annotation["type"] for annotation in annotations] == [annotation_type, annotation_type]
        assert [annotation["color"] for annotation in annotations] == pytest.approx(expected_colors, abs=0.001)


@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
def test_internal_manual_same_page_regions_keep_their_own_colors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    source = tmp_path / "manual-multicolor.pdf"
    output = tmp_path / f"manual-internal-{action}.pdf"
    _native_pdf(source)
    boxes = [
        {"id": "region-red", "page": 1, "x": 25, "y": 35, "width": 75, "height": 55, "color": "#FF0000"},
        {"id": "region-blue", "page": 1, "x": 105, "y": 35, "width": 90, "height": 55, "color": "#0000FF"},
    ]

    result = _run(monkeypatch, "internal", source, output, action=action, mode="manual", boxes=boxes)

    assert result == {"source_policy": "MANUAL_RECTANGLE", "selection_count": 0}
    assert _drawing_colors(output) == pytest.approx([(1, 0, 0), (0, 0, 1)], abs=0.001)


@pytest.mark.parametrize("rotation,cropbox", [(90, False), (0, True)])
def test_sdk_visible_geometry_preserves_rotation_and_cropbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rotation: int,
    cropbox: bool,
) -> None:
    source = tmp_path / "geometry.pdf"
    output = tmp_path / "geometry-sdk.pdf"
    _native_pdf(source, rotation=rotation, cropbox=cropbox)
    result = _run(monkeypatch, "sdk", source, output, action="strikeout", mode="smart", boxes=_box(width=230, height=100))

    document = fitz.open(output)
    assert document[0].rotation == rotation
    assert result["annotation_count"] == 1
    assert _annotations(output)[0]["type"] == "StrikeOut"
    document.close()

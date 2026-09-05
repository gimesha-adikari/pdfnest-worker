from __future__ import annotations

import io
import math
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

import app.core.studio_editor_extraction_engine as editor_engine
import app.core.studio_markup_region_ocr_engine as markup_engine
from app.jobs.cancellation import JobCancelledException


def _mixed_pdf(path: Path) -> None:
    image = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    draw.text((70, 120), "Studio Region Alpha Bravo", font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")

    document = fitz.open()
    document.new_page(width=360, height=200).insert_text((35, 70), "Native Editor Alpha Bravo", fontsize=24)
    document.new_page(width=450, height=180).insert_image(
        fitz.Rect(0, 0, 450, 180),
        stream=stream.getvalue(),
    )
    document.save(path)
    document.close()


def _boxes() -> list[dict[str, object]]:
    return [
        {"id": "scan-red", "page": 2, "x": 20, "y": 45, "width": 135, "height": 100, "color": "#FF0000"},
        {"id": "scan-blue", "page": 2, "x": 150, "y": 45, "width": 150, "height": 100, "color": "#0000FF"},
    ]


def _annotations(path: Path) -> list[dict[str, object]]:
    document = fitz.open(path)
    values = [
        {
            "page": page_index + 1,
            "type": annotation.type[1],
            "color": tuple(round(value, 4) for value in annotation.colors["stroke"]),
            "rect": tuple(round(value, 3) for value in annotation.rect),
        }
        for page_index, page in enumerate(document)
        for annotation in (page.annots() or ())
    ]
    document.close()
    return values


def _strict_studio_layout_material(layout: dict[str, object]) -> tuple[object, ...]:
    assert layout["success"] is True
    assert layout["schema_version"] == "ocr_v2_editor_layout.v1"
    assert layout["ocr_v2"] is True
    pages = layout["pages"]
    assert isinstance(pages, list) and pages
    element_ids: set[str] = set()
    material = []
    for expected_page, page in enumerate(pages, start=1):
        assert page["page_num"] == expected_page
        assert page["kind"] in {"text", "mixed", "scanned", "blank"}
        assert all(math.isfinite(float(page[key])) and float(page[key]) > 0 for key in ("width", "height"))
        elements = page["elements"]
        assert isinstance(elements, list)
        page_material = []
        for element in elements:
            assert isinstance(element["id"], str) and element["id"] and element["id"] not in element_ids
            element_ids.add(element["id"])
            assert all(math.isfinite(float(element[key])) and float(element[key]) > 0 for key in ("width", "height", "size"))
            assert all(math.isfinite(float(element[key])) for key in ("x", "y"))
            page_material.append(
                (element["id"], element["text"], element["word_ids"], element["word_geometry"], element["reading_order"])
            )
        material.append((page["page_num"], page["kind"], page["source"], page_material))
    return tuple(material)


def _selection_material(result: dict[str, object]) -> list[tuple[object, ...]]:
    return [
        (
            selection["page_index"],
            selection["word_ids"],
            selection["matched_text"],
            selection["group_rects"],
            selection["source_type"],
        )
        for selection in result["selections"]
    ]


def test_combined_selector_matrix_runs_both_boundaries_in_one_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    _mixed_pdf(source)
    branches: list[tuple[str, str]] = []

    editor_internal = editor_engine._internal_execute
    editor_sdk = editor_engine._sdk_execute
    markup_internal = markup_engine._internal_execute
    markup_sdk = markup_engine._sdk_execute

    def record_editor_internal(*args: object, **kwargs: object) -> object:
        branches.append(("editor", "internal"))
        return editor_internal(*args, **kwargs)

    def record_editor_sdk(*args: object, **kwargs: object) -> object:
        branches.append(("editor", "sdk"))
        return editor_sdk(*args, **kwargs)

    def record_markup_internal(*args: object, **kwargs: object) -> dict[str, object]:
        branches.append(("markup", "internal"))
        return markup_internal(*args, **kwargs)

    def record_markup_sdk(*args: object, **kwargs: object) -> dict[str, object]:
        branches.append(("markup", "sdk"))
        return markup_sdk(*args, **kwargs)

    monkeypatch.setattr(editor_engine, "_internal_execute", record_editor_internal)
    monkeypatch.setattr(editor_engine, "_sdk_execute", record_editor_sdk)
    monkeypatch.setattr(markup_engine, "_internal_execute", record_markup_internal)
    monkeypatch.setattr(markup_engine, "_sdk_execute", record_markup_sdk)

    matrix = [
        ("internal", "internal"),
        ("sdk", "internal"),
        ("internal", "sdk"),
        ("sdk", "sdk"),
        ("internal", "internal"),
    ]
    reference_layout = None
    reference_selection = None
    reference_annotations = None
    for index, (editor_selected, markup_selected) in enumerate(matrix):
        monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, editor_selected)
        monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, markup_selected)
        editor_progress: list[tuple[int, int, int]] = []
        markup_progress: list[tuple[int, int]] = []
        output = tmp_path / f"matrix-{index}.pdf"

        layout = editor_engine.execute_studio_editor_extraction(
            source,
            page_progress_callback=lambda done, total, page: editor_progress.append((done, total, page.page_index)),
        )
        markup = markup_engine.execute_studio_markup_region_ocr(
            source,
            output,
            boxes=_boxes(),
            action="highlight",
            mode="smart",
            progress_callback=lambda done, total: markup_progress.append((done, total)),
        )

        layout_material = _strict_studio_layout_material(layout)
        selection_material = _selection_material(markup)
        annotations = _annotations(output)
        assert editor_progress == [(1, 2, 0), (2, 2, 1)]
        assert markup_progress == [(1, 2), (2, 2)]
        assert [annotation["type"] for annotation in annotations] == ["Highlight", "Highlight"]
        assert [annotation["color"] for annotation in annotations] == pytest.approx(
            [(1, 0, 0), (0, 0, 1)],
            abs=0.001,
        )
        assert markup["selection_count"] == 2
        if markup_selected == "sdk":
            assert markup["source_policy"] == "EXTRACT_TEXT_ONCE_THEN_CANONICAL_REGION_SELECTION"
            assert markup["extraction_performed"] is True
        else:
            assert markup["source_policy"] == "OCR_V2_CANONICAL_WORDS"

        if reference_layout is None:
            reference_layout = layout_material
            reference_selection = selection_material
            reference_annotations = annotations
        else:
            assert layout_material == reference_layout
            assert selection_material == reference_selection
            assert annotations == reference_annotations

    assert branches == [
        ("editor", "internal"), ("markup", "internal"),
        ("editor", "sdk"), ("markup", "internal"),
        ("editor", "internal"), ("markup", "sdk"),
        ("editor", "sdk"), ("markup", "sdk"),
        ("editor", "internal"), ("markup", "internal"),
    ]


@pytest.mark.parametrize(("action", "annotation_type"), [("underline", "Underline"), ("strikeout", "StrikeOut")])
def test_combined_sdk_sdk_keeps_other_markup_actions_valid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    annotation_type: str,
) -> None:
    source = tmp_path / "mixed.pdf"
    output = tmp_path / f"{action}.pdf"
    _mixed_pdf(source)
    monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")

    _strict_studio_layout_material(editor_engine.execute_studio_editor_extraction(source))
    result = markup_engine.execute_studio_markup_region_ocr(
        source,
        output,
        boxes=_boxes(),
        action=action,
        mode="smart",
    )

    assert result["annotation_count"] == 2
    assert [annotation["type"] for annotation in _annotations(output)] == [annotation_type, annotation_type]
    assert [annotation["color"] for annotation in _annotations(output)] == pytest.approx(
        [(1, 0, 0), (0, 0, 1)],
        abs=0.001,
    )


def test_combined_sdk_sdk_manual_markup_remains_zero_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    output = tmp_path / "manual.pdf"
    _mixed_pdf(source)
    monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")

    _strict_studio_layout_material(editor_engine.execute_studio_editor_extraction(source))
    result = markup_engine.execute_studio_markup_region_ocr(
        source,
        output,
        boxes=_boxes(),
        action="highlight",
        mode="manual",
    )

    assert result["extraction_performed"] is False
    assert result["annotation_count"] == 2
    assert [annotation["color"] for annotation in _annotations(output)] == pytest.approx(
        [(1, 0, 0), (0, 0, 1)],
        abs=0.001,
    )


def test_invalid_selector_does_not_change_the_other_studio_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "invalid")
    monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    with pytest.raises(editor_engine.StudioEditorExtractionEngineConfigurationError):
        editor_engine.configured_studio_editor_extraction_engine()
    assert markup_engine.configured_studio_markup_region_ocr_engine() == "sdk"

    monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "invalid")
    assert editor_engine.configured_studio_editor_extraction_engine() == "sdk"
    with pytest.raises(markup_engine.StudioMarkupRegionOcrEngineConfigurationError):
        markup_engine.configured_studio_markup_region_ocr_engine()


def test_sdk_cancellation_is_operation_local_between_combined_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    _mixed_pdf(source)
    monkeypatch.setenv(editor_engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setenv(markup_engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")

    def cancelled() -> None:
        raise JobCancelledException("combined validation cancellation")

    with pytest.raises(JobCancelledException, match="combined validation cancellation"):
        editor_engine.execute_studio_editor_extraction(source, cancellation_check=cancelled)
    markup_engine.execute_studio_markup_region_ocr(
        source,
        tmp_path / "markup-after-editor-cancel.pdf",
        boxes=_boxes(),
        action="highlight",
        mode="smart",
    )

    with pytest.raises(JobCancelledException, match="combined validation cancellation"):
        markup_engine.execute_studio_markup_region_ocr(
            source,
            tmp_path / "cancelled-markup.pdf",
            boxes=_boxes(),
            action="highlight",
            mode="smart",
            cancellation_check=cancelled,
        )
    _strict_studio_layout_material(editor_engine.execute_studio_editor_extraction(source))


def test_sdk_processors_are_operation_local_instances() -> None:
    assert editor_engine._sdk_processor() is not editor_engine._sdk_processor()
    assert markup_engine._sdk_processor() is not markup_engine._sdk_processor()

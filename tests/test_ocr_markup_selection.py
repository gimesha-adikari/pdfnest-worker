from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest
from pydantic import ValidationError

import app.core.ocr_markup_engine as engine
from app.api.ocr_v2.schemas import OCRV2JobSubmitRequest, OCRV2MarkupSelection


def _write_pdf(path: Path, *, rotated: bool = False) -> None:
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 40), "original page")
    if rotated:
        page.set_rotation(90)
    document.save(path)
    document.close()


def _selection(*, rotated: bool = False) -> dict[str, object]:
    return {
        "page": 1,
        "source": "ocr",
        "coordinate_space": "pdf_points_visible_cropbox_top_left",
        "page_width": 100 if rotated else 200,
        "page_height": 200 if rotated else 100,
        "rotation": 90 if rotated else 0,
        "crop_box": [0, 0, 200, 100],
        "word_ids": ["word-1", "word-2"],
        "rects": [{"x": 20, "y": 20, "width": 80, "height": 18}],
        "text": "selected words",
    }


@pytest.mark.parametrize("action,annotation_type", [("highlight", "Highlight"), ("underline", "Underline"), ("strikeout", "StrikeOut")])
def test_browser_geometry_applies_each_markup_without_running_ocr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    annotation_type: str,
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / f"{action}.pdf"
    _write_pdf(source)
    with fitz.open(source) as original:
        source_pixels = original[0].get_pixmap(alpha=False, annots=False).samples
    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "configured_ocr_markup_engine", lambda: pytest.fail("explicit browser geometry must not consult an OCR selector"))

    result = engine.execute_ocr_markup(source, output, action=action, query="", selection=_selection())

    assert result.to_dict()["source_policy"] == "BROWSER_SELECTION_CANONICAL_PDF_POINTS"
    with fitz.open(output) as document:
        page = document[0]
        annotation = page.first_annot
        assert annotation is not None
        assert annotation.type[1] == annotation_type
        output_pixels = page.get_pixmap(alpha=False, annots=False).samples
        assert len(output_pixels) == len(source_pixels)
        assert max(abs(left - right) for left, right in zip(output_pixels, source_pixels)) <= 1
        assert sum(left != right for left, right in zip(output_pixels, source_pixels)) < max(1, int(len(source_pixels) * 0.02))
        assert document.page_count == 1


def test_browser_geometry_uses_rotated_visible_page_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "rotated.pdf"
    output = tmp_path / "rotated-output.pdf"
    _write_pdf(source, rotated=True)

    engine.execute_ocr_markup(source, output, action="highlight", query="", selection=_selection(rotated=True))

    with fitz.open(output) as document:
        assert document[0].rect.width == pytest.approx(100)
        assert document[0].rect.height == pytest.approx(200)
        assert document[0].first_annot is not None


def test_markup_job_schema_accepts_selection_without_query() -> None:
    selection = OCRV2MarkupSelection.model_validate(_selection())
    request = OCRV2JobSubmitRequest(
        request_id="selection-job",
        profile="MARKUP_V2",
        language="eng",
        source_key="jobs/input.pdf",
        owner_identity="guest:test",
        markup_action="highlight",
        markup_mode="ocr",
        markup_query=None,
        markup_selection=selection,
    )

    assert request.markup_selection is not None
    assert request.markup_selection.rects[0].width == 80


def test_markup_job_schema_still_requires_query_or_selection() -> None:
    with pytest.raises((ValidationError, ValueError)):
        OCRV2JobSubmitRequest(
            request_id="queryless-job",
            profile="MARKUP_V2",
            language="eng",
            source_key="jobs/input.pdf",
            owner_identity="guest:test",
            markup_action="highlight",
            markup_mode="ocr",
            markup_query=None,
        )

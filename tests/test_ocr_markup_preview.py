from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

import app.core.ocr_markup_engine as engine
from app.core.ocr_markup_preview import project_markup_preview
from app.core.ocr_v2.contracts import (
    Confidence,
    DocumentResult,
    LanguageMetadata,
    OCRToken,
    PageContentClassification,
    PageGeometry,
    PageProcessingSource,
    PageResult,
    PageStatus,
    Rect,
    SourceMetadata,
)


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.set_rotation(90)
    document.save(path)
    document.close()


def _canonical_preview_result() -> DocumentResult:
    tokens = (
        OCRToken(
            "word-1",
            "Sinhala",
            Rect(12, 18, 44, 11),
            confidence=Confidence(98.0, "percent", "tesseract"),
            line_id="line-1",
        ),
        OCRToken(
            "word-2",
            "heading",
            Rect(62, 18, 48, 11),
            confidence=Confidence(96.0, "percent", "tesseract"),
            line_id="line-1",
        ),
    )
    page = PageResult(
        page_index=0,
        page_id="page-0",
        geometry=PageGeometry(
            width=100,
            height=200,
            rotation=90,
            coordinate_space="pdf_points_visible_cropbox_top_left",
        ),
        content_classification=PageContentClassification.IMAGE_SCAN,
        processing_source=PageProcessingSource.OCR_RECOGNITION,
        status=PageStatus.SUCCESS,
        text="Sinhala heading",
        tokens=tokens,
        reading_order=("word-1", "word-2"),
        language=LanguageMetadata(
            requested_languages=("eng", "sin"),
            detected_languages=("sin",),
            language_status="MULTILINGUAL_DETECTED",
            requested_mode="AUTO",
        ),
        capabilities=frozenset({"TEXT", "WORD_GEOMETRY", "READING_ORDER"}),
    )
    return DocumentResult("ocr_v2.1", "preview-result", SourceMetadata("fixture", 1), (page,))


def test_markup_preview_projects_only_authorized_word_geometry(tmp_path: Path) -> None:
    source = tmp_path / "rotated-scan.pdf"
    _write_pdf(source)

    projected = project_markup_preview(_canonical_preview_result(), source)

    assert projected["schema_version"] == "ocr_v2_markup_preview.v1"
    assert projected["profile"] == "MARKUP_V2"
    assert projected["page_count"] == 1
    page = projected["pages"][0]
    assert page["selection_mode"] == "ocr"
    assert page["rotation"] == 90
    assert page["coordinate_space"] == "pdf_points_visible_cropbox_top_left"
    assert page["crop_box"] == [0.0, 0.0, 200.0, 100.0]
    assert page["reading_order"] == ["word-1", "word-2"]
    assert [word["text"] for word in page["words"]] == ["Sinhala", "heading"]
    assert [word["order"] for word in page["words"]] == [0, 1]
    assert "text" not in page


def test_markup_preview_selector_routes_to_internal_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(engine.OCR_MARKUP_ENGINE_ENV, raising=False)
    calls: list[str] = []

    def internal(_path: str | Path, **_kwargs: object) -> dict[str, str]:
        calls.append("internal")
        return {"engine": "internal"}

    monkeypatch.setattr(engine, "_preview_internal", internal)
    result = engine.execute_ocr_markup_preview(tmp_path / "input.pdf")

    assert result == {"engine": "internal"}
    assert calls == ["internal"]


def test_markup_preview_sdk_selector_does_not_call_internal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    calls: list[str] = []

    def sdk(_path: str | Path, **_kwargs: object) -> dict[str, str]:
        calls.append("sdk")
        return {"engine": "sdk"}

    monkeypatch.setattr(engine, "_preview_sdk", sdk)
    monkeypatch.setattr(engine, "_preview_internal", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("internal preview must not run")))
    result = engine.execute_ocr_markup_preview(tmp_path / "input.pdf")

    assert result == {"engine": "sdk"}
    assert calls == ["sdk"]

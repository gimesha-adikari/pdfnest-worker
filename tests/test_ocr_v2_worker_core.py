from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

from app.core.ocr_v2 import OCRProfile, OCRV2Worker, RasterPreparer, pixel_rect_to_points
from app.core.ocr_v2.adapters import TesseractAdapter
from app.core.ocr_v2.contracts import PageContentClassification, PageGeometry, Rect, ResultCapability
from app.core.ocr_v2.errors import ConfigurationError, RenderingNotEligibleError
from app.core.ocr_v2.native import NativeExtractor, NativeValidator
from app.core.ocr_v2.renderers import SearchablePdfRenderer
from app.core.ocr_v2.routing import OCRRouter, RoutePolicy
from app.core.ocr_v2.validation import validate_document


def _scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (600, 400), "white")
    ImageDraw.Draw(image).text((40, 80), "Scanned Hello 123", fill="black")
    stream = io.BytesIO()
    image.save(stream, "PNG")
    document = fitz.open()
    document.new_page(width=300, height=200).insert_image(fitz.Rect(0, 0, 300, 200), stream=stream.getvalue())
    document.save(str(path))
    document.close()


def test_pixel_geometry_uses_actual_raster_dimensions() -> None:
    geometry = PageGeometry(width=300, height=200, pixel_width=600, pixel_height=400)
    assert pixel_rect_to_points((100, 80, 300, 180), geometry) == Rect(50, 40, 100, 50)


def test_native_validator_is_conservative_for_image_pages(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    with fitz.open(path) as document:
        candidate = NativeExtractor().extract(document[0], 0)
    decision = NativeValidator().validate(candidate)
    assert decision.classification is PageContentClassification.IMAGE_SCAN
    assert decision.decision == "VISUAL_OCR_REQUIRED"


def test_native_extractor_clips_edge_word_to_visible_page() -> None:
    class EdgePage:
        rect = fitz.Rect(0, 0, 100, 100)
        rotation = 0

        def get_text(self, kind: str):
            if kind == "words":
                return [(95, 10, 120, 20, "edge", 0, 0, 0)]
            return "edge"

        def get_images(self, full: bool = True):
            return []

    candidate = NativeExtractor().extract(EdgePage(), 0)
    assert candidate.items[0]["bbox"] == [95.0, 10.0, 100.0, 20.0]


def test_tesseract_rejects_implicit_language_detection() -> None:
    with pytest.raises(ConfigurationError):
        TesseractAdapter("auto")


def test_router_uses_configured_fallback_without_product_logic() -> None:
    class Fake:
        def availability(self):
            return type("Availability", (), {"available": True})()

    plan = OCRRouter({"preferred": Fake(), "fallback": Fake()}, RoutePolicy("preferred", "fallback")).plan(
        type("Decision", (), {"decision": "VISUAL_OCR_REQUIRED", "classification": PageContentClassification.IMAGE_SCAN})(),
        OCRProfile.OCR_TEXT_V2,
    )
    assert plan.engine_id == "preferred"


def test_worker_uses_native_text_and_keeps_real_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "native.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((40, 80), "Native Hello")
    document.save(str(path))
    document.close()
    result = OCRV2Worker().process_document(path, language="eng")
    assert result.validation.valid
    assert result.pages[0].processing_source.value == "NATIVE_EXTRACTION"
    assert ResultCapability.WORD_GEOMETRY.value in result.pages[0].capabilities


def test_worker_emits_durable_page_checkpoints_in_source_order(tmp_path: Path) -> None:
    path = tmp_path / "two-pages.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((40, 80), "Page one")
    document.new_page(width=300, height=200).insert_text((40, 80), "Page two")
    document.save(str(path))
    document.close()

    checkpoints: list[tuple[int, int, int, str]] = []
    result = OCRV2Worker().process_document(
        path,
        language="eng",
        page_progress_callback=lambda done, total, page: checkpoints.append((done, total, page.page_index, page.status.value)),
    )
    assert result.validation.valid
    assert checkpoints == [(1, 2, 0, "SUCCESS"), (2, 2, 1, "SUCCESS")]


def test_worker_ocr_and_searchable_renderer_use_canonical_words(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    output = tmp_path / "searchable.pdf"
    _scanned_pdf(source)
    result = OCRV2Worker().process_document(source, language="eng", profile=OCRProfile.SEARCHABLE_PDF_V2)
    assert result.validation.valid
    assert ResultCapability.WORD_GEOMETRY.value in result.capabilities
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        assert "Scanned" in document[0].get_text("text")


def test_searchable_renderer_fails_closed_without_word_geometry(tmp_path: Path) -> None:
    source = tmp_path / "native.pdf"
    output = tmp_path / "searchable.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200)
    document.save(str(source))
    document.close()
    result = OCRV2Worker().process_document(source, language="eng")
    with pytest.raises(RenderingNotEligibleError):
        SearchablePdfRenderer().render(source, result, output)

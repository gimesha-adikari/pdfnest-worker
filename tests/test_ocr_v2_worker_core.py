from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_v2 import OCRProfile, OCRV2Worker, RasterPreparer, pixel_rect_to_points
from app.core.ocr_v2.adapters import TesseractAdapter
from app.core.ocr_v2.contracts import PageContentClassification, PageGeometry, Rect, ResultCapability
from app.core.ocr_v2.errors import ConfigurationError, RenderingNotEligibleError
from app.core.ocr_v2.image_pages import build_image_source_pdf, normalize_image
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


def test_image_source_pdf_normalizes_exif_and_preserves_order(tmp_path: Path) -> None:
    first = Image.new("RGB", (600, 400), "white")
    ImageDraw.Draw(first).text((50, 80), "First page", fill="black")
    first_path = tmp_path / "first.png"
    first.save(first_path)

    second = Image.new("RGB", (400, 600), "white")
    ImageDraw.Draw(second).text((50, 80), "Second page", fill="black")
    exif = second.getexif()
    exif[274] = 6
    second_path = tmp_path / "second.jpg"
    second.save(second_path, exif=exif)

    source = tmp_path / "ordered.pdf"
    normalized = build_image_source_pdf([first_path, second_path], source)
    assert [item.width for item in normalized] == [600, 600]
    assert [item.height for item in normalized] == [400, 400]
    with fitz.open(source) as document:
        assert len(document) == 2
        assert document[0].rect.width == pytest.approx(288)
        assert document[1].rect.width == pytest.approx(288)
        assert "First page" not in document[0].get_text()
        assert len(document[0].get_images(full=True)) == 1
        assert len(document[1].get_images(full=True)) == 1


def test_image_normalization_rejects_excessive_decoded_pixels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "oversized.png"
    Image.new("RGB", (1200, 1000), "white").save(image_path)
    monkeypatch.setenv("OCR_V2_MAX_IMAGE_PIXELS", "1000000")

    with pytest.raises(ValueError, match="OCR_V2_MAX_IMAGE_PIXELS"):
        normalize_image(image_path)


def test_searchable_renderer_preserves_variable_page_geometry_and_reading_order(tmp_path: Path) -> None:
    """Regression: each image page keeps its own geometry and OCR order."""
    images: list[Path] = []
    for index, (size, lines) in enumerate(
        [
            ((600, 400), [("tiny", 8), ("normal", 16), ("LARGE", 30)]),
            ((1000, 600), [("wide", 24), ("page", 40), ("42", 56)]),
        ]
    ):
        image = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(image)
        y = 40
        for text, font_size in lines:
            font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", font_size)
            draw.text((40, y), text, fill="black", font=font)
            y += font_size + 30
        path = tmp_path / f"variable-page-{index}.png"
        image.save(path)
        images.append(path)

    source = tmp_path / "variable-pages.pdf"
    output = tmp_path / "variable-pages-searchable.pdf"
    build_image_source_pdf(images, source)
    result = OCRV2Worker().process_document(source, language="eng", profile=OCRProfile.SEARCHABLE_PDF_V2)
    assert result.validation.valid
    assert all(page.tokens for page in result.pages)

    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(str(source)) as source_document, fitz.open(str(output)) as output_document:
        assert [page.rect for page in output_document] == [page.rect for page in source_document]
        for page_result, source_page, output_page in zip(result.pages, source_document, output_document):
            expected = " ".join(page_result.tokens_by_id[token_id].text for token_id in page_result.reading_order)
            actual = " ".join(str(word[4]) for word in output_page.get_text("words"))
            assert expected in actual
            assert output_page.get_images(full=True)
            assert source_page.get_pixmap(alpha=False).samples == output_page.get_pixmap(alpha=False).samples


def test_all_blank_image_pages_are_valid_searchable_input(tmp_path: Path) -> None:
    image_path = tmp_path / "blank.webp"
    Image.new("RGB", (300, 200), "white").save(image_path)
    source = tmp_path / "blank.pdf"
    build_image_source_pdf([image_path], source)
    result = OCRV2Worker().process_document(source, language="eng", profile=OCRProfile.SEARCHABLE_PDF_V2)
    assert result.validation.valid
    output = tmp_path / "blank-searchable.pdf"
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        assert len(document) == 1
        assert document[0].get_text("text").strip() == ""


def test_image_input_runs_tesseract_word_geometry_to_searchable_artifact(tmp_path: Path) -> None:
    image_path = tmp_path / "input.png"
    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", 96)
    draw.text((120, 180), "Searchable Image 123", fill="black", stroke_width=1, font=font)
    image.save(image_path)
    source = tmp_path / "source.pdf"
    output = tmp_path / "artifact.pdf"
    build_image_source_pdf([image_path], source)
    result = OCRV2Worker().process_document(source, language="eng", profile=OCRProfile.SEARCHABLE_PDF_V2)
    assert result.validation.valid
    assert ResultCapability.WORD_GEOMETRY.value in result.capabilities
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        extracted = document[0].get_text("text")
        assert "Searchable" in extracted
        assert "123" in extracted
        assert document[0].get_images(full=True)

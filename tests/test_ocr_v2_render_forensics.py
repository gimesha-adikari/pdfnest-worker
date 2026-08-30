from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

from app.core.ocr_v2 import OCRProfile, OCRV2Worker
from app.core.ocr_v2.diagnostics import debug_retain_failed_render_enabled, retain_failed_render_artifacts
from app.core.ocr_v2.errors import RenderingNotEligibleError
from app.core.ocr_v2.contracts import DocumentResult, LanguageMetadata, OCRToken, PageContentClassification, PageGeometry, PageProcessingSource, PageResult, PageStatus, Provenance, Rect, ResultCapability, SourceMetadata
from app.core.ocr_v2.image_pages import build_image_source_pdf, normalize_image
from app.core.ocr_v2.renderers import SearchablePdfRenderer
from app.core.ocr_v2.renderers.validation import validate_searchable_pdf_artifact


def _searchable_fixture(tmp_path: Path) -> tuple[Path, Path, object]:
    image_path = tmp_path / "fixture.png"
    image = Image.new("RGB", (800, 500), "white")
    ImageDraw.Draw(image).text((80, 180), "Forensics 123", fill="black")
    image.save(image_path)
    source = tmp_path / "source.pdf"
    build_image_source_pdf([image_path], source)
    result = OCRV2Worker().process_document(source, language="eng", profile=OCRProfile.SEARCHABLE_PDF_V2)
    assert result.validation.valid
    return image_path, source, result


def test_normalized_image_exposes_safe_source_metadata(tmp_path: Path) -> None:
    image_path = tmp_path / "oriented-rgba.png"
    image = Image.new("RGBA", (240, 120), (255, 255, 255, 255))
    exif = image.getexif()
    exif[274] = 6
    image.save(image_path, exif=exif)

    normalized = normalize_image(image_path)

    assert normalized.format == "PNG"
    assert normalized.input_mode == "RGBA"
    assert normalized.normalized_mode == "RGBA"
    assert normalized.exif_present
    assert normalized.exif_orientation == 6
    assert normalized.alpha_present
    assert normalized.width == 120
    assert normalized.height == 240


def test_renderer_logs_output_metadata_before_validation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _, source, result = _searchable_fixture(tmp_path)
    output = tmp_path / "rendered.pdf"

    with caplog.at_level("INFO"):
        SearchablePdfRenderer().render(source, result, output, job_id="forensics-job")

    output_records = [record.message for record in caplog.records if "RENDER_OUTPUT_READY" in record.message]
    assert output_records
    assert '"pdf_header_valid": true' in output_records[-1]
    assert '"image_count": 1' in output_records[-1]


def test_renderer_preserves_extraction_boundaries_for_overlapping_word_boxes(tmp_path: Path) -> None:
    image_path = tmp_path / "overlapping-boxes.png"
    Image.new("RGB", (400, 240), "white").save(image_path)
    source = tmp_path / "source.pdf"
    normalized = build_image_source_pdf([image_path], source)[0]
    geometry = PageGeometry(normalized.page_width, normalized.page_height, pixel_width=normalized.width, pixel_height=normalized.height)
    tokens = (
        OCRToken("token-a", "alpha", Rect(50, 50, 24, 10)),
        OCRToken("token-b", "beta", Rect(54, 50, 30, 10)),
    )
    page = PageResult(
        page_index=0,
        page_id="page-0",
        geometry=geometry,
        content_classification=PageContentClassification.IMAGE_SCAN,
        processing_source=PageProcessingSource.OCR_RECOGNITION,
        status=PageStatus.SUCCESS,
        text="alpha beta",
        tokens=tokens,
        reading_order=("token-a", "token-b"),
        language=LanguageMetadata(("eng",), (), "REQUESTED_ONLY", (), "NOT_DETECTED"),
        capabilities=frozenset({ResultCapability.TEXT.value, ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value}),
    )
    result = DocumentResult(
        schema_version="ocr_v2.1",
        result_id="overlap-regression",
        source=SourceMetadata(str(source), 1, source.name),
        pages=(page,),
        capabilities=frozenset({ResultCapability.TEXT.value, ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value}),
        provenance=(Provenance("overlap-regression"),),
    )
    output = tmp_path / "rendered.pdf"
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        assert [word[4] for word in document[0].get_text("words")] == ["alpha", "beta"]


def test_renderer_uses_unicode_capable_font_for_non_ascii_ocr_tokens(tmp_path: Path) -> None:
    image_path = tmp_path / "unicode-token.png"
    Image.new("RGB", (400, 240), "white").save(image_path)
    source = tmp_path / "source.pdf"
    normalized = build_image_source_pdf([image_path], source)[0]
    geometry = PageGeometry(normalized.page_width, normalized.page_height, pixel_width=normalized.width, pixel_height=normalized.height)
    token = OCRToken("token-euro", "€&", Rect(50, 50, 24, 10))
    page = PageResult(
        page_index=0,
        page_id="page-0",
        geometry=geometry,
        content_classification=PageContentClassification.IMAGE_SCAN,
        processing_source=PageProcessingSource.OCR_RECOGNITION,
        status=PageStatus.SUCCESS,
        text="€&",
        tokens=(token,),
        reading_order=(token.id,),
        language=LanguageMetadata(("eng",), (), "REQUESTED_ONLY", (), "NOT_DETECTED"),
        capabilities=frozenset({ResultCapability.TEXT.value, ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value}),
    )
    result = DocumentResult(
        schema_version="ocr_v2.1",
        result_id="unicode-font-regression",
        source=SourceMetadata(str(source), 1, source.name),
        pages=(page,),
        capabilities=frozenset({ResultCapability.TEXT.value, ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value}),
        provenance=(Provenance("unicode-font-regression"),),
    )
    output = tmp_path / "rendered.pdf"
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        assert [word[4] for word in document[0].get_text("words")] == ["€&"]


def test_validator_retains_distinct_text_and_visual_substages(tmp_path: Path) -> None:
    _, source, result = _searchable_fixture(tmp_path)

    with pytest.raises(RenderingNotEligibleError) as text_failure:
        validate_searchable_pdf_artifact(source, source, result)
    assert text_failure.value.substage == "PDF_VALIDATE_TEXT_EXTRACTION"
    assert text_failure.value.reason_code == "TEXT_EXTRACTION_MISMATCH"

    output = tmp_path / "rendered.pdf"
    SearchablePdfRenderer().render(source, result, output)
    with fitz.open(output) as document:
        document[0].insert_text((10, 20), "visible diagnostic mutation", color=(1, 0, 0), overlay=True)
        document.save(str(tmp_path / "mutated.pdf"))
    with pytest.raises(RenderingNotEligibleError) as visual_failure:
        validate_searchable_pdf_artifact(source, tmp_path / "mutated.pdf", result)
    assert visual_failure.value.substage == "PDF_VALIDATE_VISUAL_RASTER"
    assert visual_failure.value.reason_code == "VISIBLE_RASTER_MISMATCH"


def test_valid_pdf_exists_before_validator_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, result = _searchable_fixture(tmp_path)
    output = tmp_path / "rendered-before-validation.pdf"

    def fail_validation(*args: object, **kwargs: object) -> None:
        raise RenderingNotEligibleError("forced forensic validation failure", substage="PDF_VALIDATE_VISUAL_RASTER", reason_code="VISIBLE_RASTER_MISMATCH")

    monkeypatch.setattr("app.core.ocr_v2.renderers.searchable_pdf.validate_searchable_pdf_artifact", fail_validation)
    with pytest.raises(RenderingNotEligibleError):
        SearchablePdfRenderer().render(source, result, output)
    assert output.read_bytes()[:5] == b"%PDF-"
    with fitz.open(output) as document:
        assert len(document) == 1
        assert document[0].get_images(full=True)


def test_failed_render_retention_requires_explicit_local_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    source.write_bytes(b"source-forensics")
    output.write_bytes(b"%PDF-1.7\nforensics")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("OCR_V2_DEBUG_RETAIN_FAILED_RENDER", raising=False)
    assert not debug_retain_failed_render_enabled()
    assert retain_failed_render_artifacts(job_id="disabled", source_pdf=source, output_pdf=output) is None

    monkeypatch.setenv("OCR_V2_DEBUG_RETAIN_FAILED_RENDER", "true")
    diagnostic_root = tmp_path / "diagnostics"
    monkeypatch.setenv("OCR_V2_DEBUG_DIAGNOSTIC_DIR", str(diagnostic_root))
    retained = retain_failed_render_artifacts(job_id="enabled", source_pdf=source, output_pdf=output, metadata=[{"page_index": 0, "token_count": 2}])
    assert retained == diagnostic_root / "enabled"
    assert (retained / "source-normalized.pdf").read_bytes() == source.read_bytes()
    assert (retained / "rendered-output.pdf").read_bytes() == output.read_bytes()
    assert "token_count" in (retained / "metadata.json").read_text(encoding="utf-8")

    for environment in ("canary", "staging", "production"):
        monkeypatch.setenv("APP_ENV", environment)
        blocked = retain_failed_render_artifacts(job_id=environment, source_pdf=source, output_pdf=output, metadata=[])
        assert blocked is None
        assert not (diagnostic_root / environment).exists()

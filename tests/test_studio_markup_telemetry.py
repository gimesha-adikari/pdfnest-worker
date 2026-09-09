from __future__ import annotations

import json
import logging
from pathlib import Path

import pymupdf as fitz
import pytest

from app.api.tools.markup import document
from app.core.ocr_v2.contracts import (
    DocumentResult,
    OCRToken,
    PageContentClassification,
    PageGeometry,
    PageProcessingSource,
    PageResult,
    PageStatus,
    Rect,
    SourceMetadata,
)
from app.core.studio_markup_telemetry import (
    StudioMarkupProcessingTelemetry,
    emit_studio_markup_processing_summary,
)


def _source_result(page_count: int, page_indexes: tuple[int, ...], source: PageProcessingSource) -> DocumentResult:
    pages = tuple(
        PageResult(
            page_index=page_index,
            page_id=f"page-{page_index}",
            geometry=PageGeometry(300, 300),
            content_classification=(
                PageContentClassification.TEXT_NATIVE
                if source is PageProcessingSource.NATIVE_EXTRACTION
                else PageContentClassification.MIXED
            ),
            processing_source=source,
            status=PageStatus.SUCCESS,
            text="Telemetry target",
            tokens=(OCRToken(id=f"word-{page_index}", text="Telemetry", bbox=Rect(10, 10, 80, 20)),),
            reading_order=(f"word-{page_index}",),
            capabilities=frozenset({"WORD_GEOMETRY"}),
        )
        for page_index in page_indexes
    )
    return DocumentResult(
        schema_version="ocr_v2.1",
        result_id="telemetry-result",
        source=SourceMetadata(source_id="telemetry-source", page_count=page_count),
        pages=pages,
    )


def _pdf(path: Path, page_count: int = 12) -> None:
    document_handle = fitz.open()
    for _ in range(page_count):
        page = document_handle.new_page(width=300, height=300)
        page.insert_text((10, 25), "Telemetry target", fontsize=12)
    document_handle.save(path)
    document_handle.close()


def _boxes(page_numbers: tuple[int, ...]) -> list[dict[str, object]]:
    return [
        {
            "id": f"region-{index}",
            "page": page_number,
            "x": 5,
            "y": 5,
            "width": 120,
            "height": 40,
            "color": "#FFFF00",
        }
        for index, page_number in enumerate(page_numbers)
    ]


@pytest.mark.parametrize(
    ("mode", "source", "page_numbers", "expected_native", "expected_ocr", "expected_fallback"),
    [
        ("smart", PageProcessingSource.NATIVE_EXTRACTION, (1,), 1, 0, 0),
        ("smart", PageProcessingSource.NATIVE_EXTRACTION, (1, 1, 1), 1, 0, 0),
        ("smart", PageProcessingSource.NATIVE_EXTRACTION, (1, 6), 2, 0, 0),
        ("ocr", PageProcessingSource.OCR_RECOGNITION, (1,), 0, 1, 0),
        ("ocr", PageProcessingSource.OCR_RECOGNITION, (1, 1, 1), 0, 1, 0),
        ("ocr", PageProcessingSource.OCR_RECOGNITION, (1, 6), 0, 2, 0),
        ("ocr", PageProcessingSource.OCR_RECOGNITION, (1, 5, 12), 0, 3, 0),
        ("ocr", PageProcessingSource.OCR_RECOGNITION, tuple(range(1, 13)), 0, 12, 0),
    ],
)
def test_internal_studio_telemetry_counts_unique_page_contexts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    source: PageProcessingSource,
    page_numbers: tuple[int, ...],
    expected_native: int,
    expected_ocr: int,
    expected_fallback: int,
) -> None:
    input_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _pdf(input_path)

    class FakeWorker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def process_document(self, _path: str, **kwargs: object) -> DocumentResult:
            page_indexes = tuple(sorted({page - 1 for page in page_numbers}))
            result = _source_result(12, page_indexes, source)
            callback = kwargs["page_progress_callback"]
            assert callable(callback)
            for done, page in enumerate(result.pages, start=1):
                callback(done, len(result.pages), page)
            return result

    monkeypatch.setattr(document, "OCRV2Worker", FakeWorker)
    result = document.process_markup_pdf_v2_regions(
        str(input_path),
        str(output_path),
        boxes=_boxes(page_numbers),
        action="highlight",
        mode=mode,
    )

    telemetry = result["_processing_telemetry"]
    assert telemetry == {
        "source_page_count": 12,
        "region_count": len(page_numbers),
        "affected_page_count": len(set(page - 1 for page in page_numbers)),
        "selected_page_indexes": sorted(set(page - 1 for page in page_numbers)),
        "native_page_context_count": expected_native,
        "ocr_page_context_count": expected_ocr,
        "smart_ocr_fallback_page_count": expected_fallback,
        "processed_page_context_count": len(set(page - 1 for page in page_numbers)),
    }


def test_smart_telemetry_counts_ocr_routing_as_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    input_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _pdf(input_path)

    class FakeWorker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def process_document(self, _path: str, **kwargs: object) -> DocumentResult:
            result = _source_result(12, (0,), PageProcessingSource.OCR_RECOGNITION)
            callback = kwargs["page_progress_callback"]
            assert callable(callback)
            callback(1, 1, result.pages[0])
            return result

    monkeypatch.setattr(document, "OCRV2Worker", FakeWorker)
    result = document.process_markup_pdf_v2_regions(
        str(input_path),
        str(output_path),
        boxes=_boxes((1,)),
        action="highlight",
        mode="smart",
    )

    telemetry = result["_processing_telemetry"]
    assert telemetry["native_page_context_count"] == 0
    assert telemetry["ocr_page_context_count"] == 1
    assert telemetry["smart_ocr_fallback_page_count"] == 1


def test_sdk_page_source_records_are_aggregated_without_text() -> None:
    telemetry = StudioMarkupProcessingTelemetry(mode="ocr", region_count=3)
    telemetry.observe_page_source({"page_index": 0, "source_type": "ocr", "selected_text": "PRIVATE"})
    telemetry.observe_page_source({"page_index": 0, "source_type": "ocr", "selected_text": "PRIVATE"})
    assert telemetry.summary(source_page_count=12) == {
        "source_page_count": 12,
        "region_count": 3,
        "affected_page_count": 1,
        "selected_page_indexes": [0],
        "native_page_context_count": 0,
        "ocr_page_context_count": 1,
        "smart_ocr_fallback_page_count": 0,
        "processed_page_context_count": 1,
    }


def test_summary_log_is_aggregate_and_contains_no_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pdfnest.ocr_v2")
    emit_studio_markup_processing_summary(
        job_id="job-123",
        action="highlight",
        mode="ocr",
        engine="internal",
        summary={
            "source_page_count": 12,
            "region_count": 1,
            "affected_page_count": 1,
            "selected_page_indexes": [0],
            "native_page_context_count": 0,
            "ocr_page_context_count": 1,
            "smart_ocr_fallback_page_count": 0,
            "processed_page_context_count": 1,
        },
    )

    message = caplog.records[-1].getMessage()
    assert message.startswith("STUDIO_MARKUP_PROCESSING_SUMMARY ")
    payload = json.loads(message.split(" ", 1)[1])
    assert payload["event"] == "studio_markup_processing_summary"
    assert payload["job_id"] == "job-123"
    assert "PRIVATE" not in message
    assert "selected_text" not in message

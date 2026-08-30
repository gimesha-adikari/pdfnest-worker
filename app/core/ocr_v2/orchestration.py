"""Page-scoped OCR V2 orchestration."""

from __future__ import annotations

import time
import uuid
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pymupdf as fitz

from .adapters import EngineAdapter, PPOCRv6MediumAdapter, TesseractAdapter
from .contracts import (
    DocumentResult,
    LanguageMetadata,
    PageContentClassification,
    PageProcessingSource,
    PageResult,
    PageStatus,
    Provenance,
    SourceMetadata,
    UnnormalizedPageOutput,
)
from .errors import LanguageDetectionUncertainError, OCRCancellationError, OCRTimeoutError
from .geometry import RasterPreparer, page_geometry_from_pdf
from .language_policy import BoundedLanguageDetector, LanguageDecisionStatus, OCRLanguageMode, OCRLanguagePolicy
from .native import NativeDecision, NativeExtractor, NativeValidator
from .normalization import normalize_page_output
from .routing import OCRRouter, RoutePolicy
from .telemetry import emit
from .validation import OCRProfile, validate_document


CancellationCheck = Callable[[], None]
PageProgressCallback = Callable[[int, int, PageResult], None]


def _check(cancel: CancellationCheck | None) -> None:
    if cancel is not None:
        cancel()


class OCRV2Worker:
    """Coordinates native extraction, routing, adapters, normalization and validation."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, EngineAdapter] | None = None,
        raster_preparer: RasterPreparer | None = None,
        native_extractor: NativeExtractor | None = None,
        native_validator: NativeValidator | None = None,
        route_policy: RoutePolicy | None = None,
        max_raster_pixels: int | None = None,
    ) -> None:
        self.adapters = dict(adapters or {
            "tesseract_v2": TesseractAdapter("eng"),
            "ppocrv6_medium_v2": PPOCRv6MediumAdapter(),
        })
        self.raster_preparer = raster_preparer or RasterPreparer(200)
        self.native_extractor = native_extractor or NativeExtractor()
        self.native_validator = native_validator or NativeValidator()
        self.router = OCRRouter(self.adapters, route_policy)
        self.max_raster_pixels = max_raster_pixels

    def _native_output(self, candidate: object) -> UnnormalizedPageOutput:
        native = candidate  # keep the small conversion explicit at this boundary
        return UnnormalizedPageOutput(
            page_id=native.page_id,
            text=native.text,
            items=tuple(native.items),
            coordinate_space="pdf_points_visible_cropbox_top_left",
            provenance=Provenance("pymupdf_native_extractor", source="pymupdf-native"),
            raw_output={"text": native.text, "items": list(native.items)},
        )

    def process_document(
        self,
        pdf_path: str | Path,
        *,
        password: str | None = None,
        language: str = "eng",
        language_mode: str | None = None,
        languages: Sequence[str] | None = None,
        language_usage: Mapping[str, float] | None = None,
        profile: OCRProfile = OCRProfile.OCR_TEXT_V2,
        cancellation_check: CancellationCheck | None = None,
        page_timeout_seconds: float | None = None,
        page_progress_callback: PageProgressCallback | None = None,
    ) -> DocumentResult:
        policy = OCRLanguagePolicy.from_request(language, mode=language_mode, languages=languages)
        tess_adapter = self.adapters.get("tesseract_v2")
        if isinstance(tess_adapter, TesseractAdapter) and policy.mode is OCRLanguageMode.EXPLICIT and tess_adapter.languages != policy.engine_expression:
            self.adapters["tesseract_v2"] = TesseractAdapter(policy.engine_expression, timeout=tess_adapter.timeout, tessdata_dir=str(tess_adapter.tessdata_dir) if tess_adapter.tessdata_dir else None)
        source_path = Path(pdf_path)
        pages: list[PageResult] = []
        provenance: list[Provenance] = []
        with fitz.open(str(source_path)) as document:
            if document.needs_pass:
                if not password or document.authenticate(password) <= 0:
                    raise ValueError("PDF password authentication failed")
            source = SourceMetadata(source_id=str(source_path.resolve()), page_count=len(document), filename=source_path.name)
            for page_index, page in enumerate(document):
                _check(cancellation_check)
                started = time.monotonic()
                page_id = f"page-{page_index}"
                emit("PAGE_START", page_index=page_index, page_id=page_id)
                candidate = None
                decision = None
                page_result: PageResult
                try:
                    candidate = self.native_extractor.extract(page, page_index)
                    decision = self.native_validator.validate(candidate)
                    if decision.decision == NativeDecision.TRUST_NATIVE or decision.classification.value in {"BLANK", "NEAR_BLANK"} and not candidate.text.strip():
                        output = self._native_output(candidate)
                        geometry = page_geometry_from_pdf(page)
                        normalized = normalize_page_output(output, page_index=page_index, geometry=geometry, classification=decision.classification, processing_source=PageProcessingSource.NATIVE_EXTRACTION, language=LanguageMetadata(policy.languages, (), "REQUESTED", (), "NOT_DETECTED", policy.mode.value))
                    else:
                        route = self.router.plan(decision, profile)
                        raster = self.raster_preparer.prepare(page)
                        if self.max_raster_pixels is not None and raster.image.width * raster.image.height > self.max_raster_pixels:
                            raise ValueError(
                                f"page raster pixel count {raster.image.width * raster.image.height} "
                                f"exceeds configured structured OCR limit {self.max_raster_pixels}"
                            )
                        emit("RASTER_READY", page_index=page_index, width=raster.image.width, height=raster.image.height)
                        page_policy = policy
                        detection = None
                        if policy.mode is OCRLanguageMode.AUTO:
                            from app.api.tools.ocr.languages import get_installed_tesseract_languages

                            installed = tuple(code for code in get_installed_tesseract_languages() if code in {"eng", "sin", "tam"})
                            candidates = policy.languages or installed or ("eng",)
                            sample = _language_detection_sample(raster.image)
                            detector = BoundedLanguageDetector(
                                max_probes=int(os.environ.get("OCR_MAX_AUTO_PROBES", "5")),
                                min_confidence=float(os.environ.get("OCR_AUTO_MIN_CONFIDENCE", "45")),
                                usage=language_usage,
                            )

                            def probe(candidate: tuple[str, ...]):
                                probe_adapter = TesseractAdapter("+".join(candidate))
                                return probe_adapter.probe(sample, candidate)

                            detection = detector.detect(candidates, probe)
                            if detection.policy is None:
                                raise LanguageDetectionUncertainError(detection.reason)
                            page_policy = detection.policy
                        if isinstance(self.adapters.get("tesseract_v2"), TesseractAdapter) and page_policy.mode is OCRLanguageMode.EXPLICIT:
                            base_adapter = self.adapters["tesseract_v2"]
                            if isinstance(base_adapter, TesseractAdapter) and base_adapter.languages != page_policy.engine_expression:
                                self.adapters["tesseract_v2"] = TesseractAdapter(page_policy.engine_expression, timeout=base_adapter.timeout, tessdata_dir=str(base_adapter.tessdata_dir) if base_adapter.tessdata_dir else None)
                        if policy.mode is OCRLanguageMode.AUTO and route.engine_id != "tesseract_v2":
                            # AUTO detection is a bounded Tesseract probe today;
                            # do not silently pass an unresolved policy to a
                            # different engine that cannot report its choice.
                            route = replace(route, engine_id="tesseract_v2")
                        adapter = self.adapters[route.engine_id or ""]
                        if not adapter.readiness():
                            adapter.initialize()
                        if not adapter.readiness():
                            raise RuntimeError(f"adapter {route.engine_id} did not reach readiness")
                        output = adapter.recognize_page(page_id, raster)
                        geometry = raster.geometry
                        normalized = normalize_page_output(
                            output,
                            page_index=page_index,
                            geometry=geometry,
                            classification=decision.classification,
                            processing_source=PageProcessingSource.OCR_RECOGNITION,
                            language=LanguageMetadata(
                                policy.languages,
                                page_policy.languages,
                                detection.status.value if detection else LanguageDecisionStatus.REQUESTED.value,
                                detection.scripts if detection else (),
                                "DETECTED" if detection else "NOT_DETECTED",
                                policy.mode.value,
                                detection.confidence if detection else None,
                                detection.reason if detection else None,
                            ),
                        )
                        if output.provenance:
                            provenance.append(output.provenance)
                    if page_timeout_seconds is not None and time.monotonic() - started > page_timeout_seconds:
                        raise OCRTimeoutError(f"page {page_index} exceeded {page_timeout_seconds}s")
                    checked = replace(normalized, validation=validate_document_placeholder(normalized, profile))
                    pages.append(checked)
                    page_result = checked
                    emit("PAGE_COMMIT_DONE", page_index=page_index, elapsed_seconds=time.monotonic() - started)
                except OCRCancellationError:
                    raise
                except Exception as exc:
                    # Preserve the worker's existing cooperative cancellation
                    # exception instead of turning a client disconnect into
                    # an ordinary failed page.
                    if exc.__class__.__name__ == "JobCancelledException":
                        raise
                    emit("PAGE_FAILED", page_index=page_index, error=type(exc).__name__, message=str(exc))
                    page_result = PageResult(page_index=page_index, page_id=page_id, geometry=page_geometry_from_pdf(page), content_classification=decision.classification if decision is not None else PageContentClassification.UNKNOWN, processing_source=PageProcessingSource.NONE, status=PageStatus.FAILED, text="", failure_code=type(exc).__name__, failure_message=str(exc))
                    pages.append(page_result)
                if page_progress_callback is not None:
                    page_progress_callback(len(pages), len(document), page_result)
        capabilities = frozenset(capability for page in pages for capability in page.capabilities)
        result = DocumentResult(schema_version="ocr_v2.1", result_id=str(uuid.uuid4()), source=source, pages=tuple(pages), capabilities=capabilities, provenance=tuple({item.producer_id: item for item in provenance}.values()))
        return validate_document(result, profile)


def validate_document_placeholder(page: PageResult, profile: OCRProfile):
    """Page validation without forcing a page failure into a document exception."""
    from .validation import _page_issues

    issues = tuple(_page_issues(page, profile))
    from .contracts import Validation

    return Validation(not issues, issues)


def _language_detection_sample(image: object):
    """Build a bounded representative sample from upper, middle, and lower regions."""
    from PIL import Image

    if not isinstance(image, Image.Image):
        return image
    width, height = image.size
    band = max(1, height // 4)
    bands = [image.crop((0, start, width, min(height, start + band))) for start in (0, max(0, (height - band) // 2), max(0, height - band))]
    sample = Image.new("RGB", (width, sum(band_image.height for band_image in bands)), "white")
    offset = 0
    for band_image in bands:
        sample.paste(band_image.convert("RGB"), (0, offset))
        offset += band_image.height
    return sample

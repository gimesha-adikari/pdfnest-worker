"""SDK release gate and exact-source parity; no oversized native renders."""
import importlib
import sys
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


FACTORIES = [
    ("ocr_text_engine", "OCRTextEngineUnavailableError"),
    ("document_extraction_engine", "DocumentExtractionEngineUnavailableError"),
    ("searchable_pdf_engine", "SearchablePdfEngineUnavailableError"),
    ("ocr_markup_engine", "OcrMarkupEngineUnavailableError"),
    ("editor_ocr_engine", "EditorOcrEngineUnavailableError"),
    ("legacy_editor_ocr_engine", "LegacyEditorOcrEngineUnavailableError"),
    ("legacy_markup_ocr_engine", "LegacyMarkupOcrEngineUnavailableError"),
    ("studio_editor_extraction_engine", "StudioEditorExtractionEngineUnavailableError"),
    ("studio_markup_region_ocr_engine", "StudioMarkupRegionOcrEngineUnavailableError"),
    ("pdf_to_word_ocr_engine", "PdfToWordOcrEngineUnavailableError"),
    ("pdf_to_markdown_engine", "PdfToMarkdownEngineUnavailableError"),
]


@pytest.mark.parametrize("module,error", FACTORIES)
def test_unguarded_sdk08_cannot_construct_processor(module, error, monkeypatch):
    engine = importlib.import_module("app.core." + module)
    old_sdk = ModuleType("platen_document")
    calls = []
    old_sdk.DocumentProcessor = lambda *args, **kw: calls.append("constructed")
    old_sdk.EngineConfiguration = lambda **kw: SimpleNamespace(**kw)
    old_sdk.RasterDpiMetadataPolicy = SimpleNamespace(OMIT_DPI="omit_dpi")
    monkeypatch.setitem(sys.modules, "platen_document", old_sdk)
    with pytest.raises(getattr(engine, error)):
        engine._sdk_processor()
    assert calls == []


@pytest.mark.parametrize("module,error", FACTORIES)
def test_actual_pinned_sdk08_is_release_blocked(module, error):
    if os.getenv("F11_SDK_CANDIDATE_ROOT"):
        pytest.skip("pinned package qualification runs separately from source candidate")
    import importlib.metadata
    import platen_document
    assert importlib.metadata.version("platen-document") == "0.1.8"
    assert not hasattr(platen_document, "RasterAdmissionError")
    engine = importlib.import_module("app.core." + module)
    with pytest.raises(getattr(engine, error)):
        engine._sdk_processor()


# Literal vectors are independent of either estimator implementation.
VECTORS = [
    ((0, 0, 595.276, 841.89), 200, None, True),
    ((0, 0, 612, 792), 300, None, True),
    ((0, 0, 841.89, 595.276), 200, None, True),
    ((10.2, 20.1, 110.3, 220.4), 72, None, True),
    ((0, 0, 2498, 3998), 72, None, True),
    ((0, 0, 2499, 3998), 72, None, False),
    ((0, 0, 98, 98), 72, 9999, False),
    ((0, 0, 98, 98), 72, 10000, True),
    ((0, 0, 100_000, 100_000), 200, 100_000_000, False),
    ((0, 0, 32766, 1), 72, None, True),
    ((0, 0, 32767, 1), 72, None, False),
    ((0, 0, 1, 32767), 72, None, False),
    ((0, 0, 0, 10), 200, None, False),
    ((0, 0, -1, 10), 200, None, False),
    ((0, 0, float("nan"), 10), 200, None, False),
    ((0, 0, float("inf"), 10), 200, None, False),
    ((0, 0, 1e300, 10), 200, None, False),
    ((0, 0, 10, 10), 601, None, False),
    ((0, 0, 10, 10), True, None, False),
]


def candidate_sdk():
    root = os.getenv("F11_SDK_CANDIDATE_ROOT")
    if not root:
        pytest.skip("exact accepted SDK source is required for integration qualification")
    import platen_document
    assert Path(platen_document.__file__).resolve() == Path(root).resolve() / "src/platen_document/__init__.py"
    return platen_document


@pytest.mark.parametrize("bounds,dpi,limit,admitted", VECTORS)
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_exact_sdk_candidate_admission_parity(bounds, dpi, limit, admitted, rotation):
    candidate_sdk()
    from platen_document.engine import geometry as sdk_geometry
    from app.core.ocr_v2 import geometry as worker_geometry
    x0, y0, x1, y1 = bounds
    page = SimpleNamespace(rect=SimpleNamespace(x0=x0,y0=y0,x1=x1,y1=y1), rotation=rotation)
    decisions = []
    for geometry in (worker_geometry, sdk_geometry):
        try:
            decision = geometry.estimate_raster(page, dpi=dpi, max_raster_pixels=limit)
        except ValueError:
            decisions.append(None)
        else:
            decisions.append((decision.width, decision.height, decision.limit))
    assert (decisions[0] is not None) is admitted
    assert decisions[0] == decisions[1]


@pytest.mark.parametrize("module,error", FACTORIES)
def test_candidate_receives_worker_budget_and_preserves_fatal_type(module, error, tmp_path, monkeypatch):
    sdk = candidate_sdk()
    from platen_document.engine import orchestration
    from app.core.ocr_v2.geometry import raster_policy
    from app.core.ocr_v2.errors import RasterAdmissionError
    engine = importlib.import_module("app.core." + module)
    processor = engine._sdk_processor()
    page = SimpleNamespace(rect=SimpleNamespace(x0=0,y0=0,x1=100,y1=100,width=100,height=100), rotation=0)
    page.get_text = lambda kind: "" if kind == "text" else []
    page.get_images = lambda **kw: [(1,)]
    page.get_image_rects = lambda *args: []
    calls = []
    def forbidden(**kwargs):
        calls.append("rendered")
        raise AssertionError("native renderer reached")
    page.get_pixmap = forbidden
    class Document:
        needs_pass = False
        def __len__(self): return 1
        def __getitem__(self, index): return page
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(orchestration.fitz, "open", lambda *args: Document())
    monkeypatch.setattr(orchestration.OCRRouter, "_available", lambda *args: True)
    with raster_policy(100), pytest.raises(RasterAdmissionError):
        processor.extract_text(tmp_path / "source.pdf")
    assert calls == []


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("clip,admitted", [((20.2,30.5,70.9,80.1), True), ((200,200,250,250), False), ((0,0,0,10), False)])
def test_sdk_candidate_clip_parity(rotation, clip, admitted):
    candidate_sdk()
    from platen_document.engine import geometry as sdk
    from app.core.ocr_v2 import geometry as worker
    import pymupdf as fitz
    page = SimpleNamespace(rect=fitz.Rect(10.2,20.1,110.3,220.4), rotation=rotation)
    results = []
    for geometry in (worker, sdk):
        try:
            value = geometry.estimate_raster(page, dpi=72, clip=fitz.Rect(*clip))
        except ValueError:
            results.append(None)
        else:
            results.append((value.width,value.height))
    assert (results[0] is not None) is admitted
    assert results[0] == results[1]


def test_word_selector_cannot_reclassify_typed_fatal_rejection(monkeypatch):
    from app.core import pdf_to_word_ocr_engine as engine
    from app.core.ocr_v2.errors import RasterAdmissionError
    def reject(*args, **kw):
        raise RasterAdmissionError("Document raster exceeds safety limits")
    monkeypatch.setattr(engine, "_sdk_processor", lambda **kw: SimpleNamespace(extract_document=reject))
    monkeypatch.setenv("PDF_TO_WORD_OCR_ENGINE", "sdk")
    with pytest.raises(RasterAdmissionError):
        engine.execute_pdf_to_word_ocr("fake.pdf", language="eng")


@pytest.mark.parametrize("error_name", ["RasterAdmissionError", "ConfigurationError"])
def test_legacy_markup_retains_typed_rejection(error_name, monkeypatch):
    from app.core import legacy_markup_ocr_engine as engine
    from app.core.ocr_v2 import errors
    error = getattr(errors, error_name)("safe rejection")
    def reject(*args, **kwargs):
        raise error
    monkeypatch.setattr(engine, "_sdk_processor", lambda: SimpleNamespace(extract_text=reject))
    monkeypatch.setattr(engine, "_sdk_profile", lambda: "profile")
    monkeypatch.setenv("LEGACY_MARKUP_OCR_ENGINE", "sdk")
    with pytest.raises(type(error)) as caught:
        engine.execute_legacy_markup("fake.pdf", "out.pdf", boxes=[], action="highlight", mode="ocr")
    assert caught.value is error


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_searchable_validation_retains_cancellation_after_ocr(selected, monkeypatch):
    from app.core import searchable_pdf_engine as engine
    from app.core.ocr_v2.geometry import render_page_pixmap
    from app.jobs.cancellation import JobCancelledException
    cancelled = False
    calls = []
    page = SimpleNamespace(rect=SimpleNamespace(x0=0,y0=0,x1=10,y1=10), rotation=0)
    page.get_pixmap = lambda **kw: calls.append("native")
    def check():
        if cancelled:
            raise JobCancelledException("cancelled")
    def after(result):
        nonlocal cancelled
        cancelled = True
    def render(*args, **kwargs):
        render_page_pixmap(page, dpi=72)
    worker = SimpleNamespace(process_document=lambda *a, **kw: object())
    monkeypatch.setattr(engine, "_internal_worker", lambda: worker)
    monkeypatch.setattr(engine, "_internal_renderer", lambda: SimpleNamespace(render=render))
    monkeypatch.setattr(engine, "_sdk_processor", lambda: SimpleNamespace(
        extract_text=lambda *a, **kw: object(), make_searchable_pdf=render))
    monkeypatch.setattr(engine, "_sdk_profile", lambda: "profile")
    monkeypatch.setenv("SEARCHABLE_PDF_ENGINE", selected)
    with pytest.raises(JobCancelledException):
        engine.execute_searchable_pdf("fake.pdf", "out.pdf", language="eng",
                                      cancellation_check=check, after_ocr=after)
    assert calls == []


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_actual_visual_validator_preserves_job_cancellation(selected, monkeypatch):
    from app.core.ocr_v2.geometry import raster_policy
    from app.core.ocr_v2.renderers import validation
    from app.jobs.cancellation import JobCancelledException
    from app.core.sdk_raster import guard_sdk_processor
    page = SimpleNamespace(rect=SimpleNamespace(x0=0,y0=0,x1=10,y1=10,width=10,height=10), rotation=0)
    page.get_images = lambda **kw: [(1,)]
    page.get_image_info = lambda: []
    page.get_text = lambda *args: []
    calls = []
    page.get_pixmap = lambda **kw: calls.append("native")
    class Document:
        def __len__(self): return 1
        def __iter__(self): return iter([page])
        def close(self): pass
    result = SimpleNamespace(pages=[SimpleNamespace(tokens=(), reading_order=())])
    def check():
        raise JobCancelledException("cancelled")
    if selected == "sdk":
        candidate_sdk()
        from platen_document.engine.renderers import validation
    monkeypatch.setattr(validation.fitz, "open", lambda *a: Document())
    def validate():
        return validation.validate_searchable_pdf_artifact("source", "output", result)
    processor = guard_sdk_processor(lambda: SimpleNamespace(validate=validate)) if selected == "sdk" else SimpleNamespace(validate=validate)
    with raster_policy(None, check), pytest.raises(JobCancelledException):
        processor.validate()
    assert calls == []


def test_candidate_page_scoped_preparer_retains_selected_dpi_and_worker_cap():
    candidate_sdk()
    import io
    from PIL import Image
    from platen_document.engine.geometry import RasterPreparer
    from app.core.pdf_to_word_ocr_engine import _PageScopedRasterPreparer
    from app.core.ocr_v2.geometry import raster_policy
    from app.core.ocr_v2.errors import RasterAdmissionError
    from app.core.sdk_raster import guard_sdk_processor
    page = SimpleNamespace(number=0, rotation=0,
                           rect=SimpleNamespace(x0=0,y0=0,x1=98,y1=98,width=98,height=98))
    stream = io.BytesIO()
    with Image.new("RGB", (98,98), "white") as image:
        image.save(stream, "PNG")
    calls = []
    def render(**kwargs):
        calls.append(kwargs["matrix"].a)
        return SimpleNamespace(width=98,height=98,tobytes=lambda *a: stream.getvalue())
    page.get_pixmap = render
    scoped = _PageScopedRasterPreparer((72,), RasterPreparer)
    processor = guard_sdk_processor(lambda: SimpleNamespace(prepare=scoped.prepare))
    with raster_policy(9999), pytest.raises(RasterAdmissionError):
        processor.prepare(page)
    assert calls == []
    with raster_policy(10000):
        raster = processor.prepare(page)
    assert raster.dpi == 72
    assert calls == [1.0]
    raster.image.close()

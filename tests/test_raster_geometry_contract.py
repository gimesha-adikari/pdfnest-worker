"""Reviewed raster geometry contract, ported with literal small/fake vectors."""
from types import SimpleNamespace
import pytest
from app.core.ocr_v2.errors import RasterAdmissionError, ConfigurationError, OCRCancellationError
from app.core.ocr_v2.geometry import RasterPreparer, estimate_raster, render_page_pixmap, raster_policy

class FakeDocument:
    def __init__(self, pages=()):
        self.pages = pages
        self.needs_pass = False

    def __len__(self):
        return len(self.pages)

    def __iter__(self):
        return iter(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass

    def save(self, path, **kwargs):
        from pathlib import Path
        Path(path).write_bytes(b"task-owned partial PDF")


class NeverRenderPage:
    def __init__(self, bounds=(0, 0, 100_000, 100_000), rotation=0):
        x0, y0, x1, y1 = bounds
        self.rect = SimpleNamespace(x0=x0, y0=y0, x1=x1, y1=y1,
                                    width=x1-x0, height=y1-y0)
        self.rotation = rotation
        self.calls = 0

    def get_pixmap(self, **kwargs):
        self.calls += 1
        raise AssertionError("native rasterization must not begin")


def test_small_native_markdown_preserves_explicit_150dpi_metadata(tmp_path, monkeypatch):
    import pymupdf as fitz
    from app.api.tools.markdown import service
    path = tmp_path / "tiny.pdf"
    with fitz.open() as doc:
        doc.new_page(width=72, height=72)
        doc.save(path)
    monkeypatch.setattr(service, "validate_ocr_language", lambda *args: None)
    monkeypatch.setattr(service, "classify_page_content", lambda *args: "SCANNED")
    resolutions = []
    def recognize(pixmap, **kwargs):
        resolutions.append((pixmap.xres, pixmap.yres))
        return []
    monkeypatch.setattr(service.OCREngine, "ocr_pixmap", recognize)
    service.convert_pdf_to_markdown(str(path))
    assert resolutions == [(150, 150)]


def test_metadata_estimates_without_native_render(tmp_path, monkeypatch):
    from app.core.ocr_v2.renderers import searchable_pdf as renderer
    page = NeverRenderPage((0, 0, 100, 200))
    page.get_image_info = lambda: []
    page.get_images = lambda **kw: []
    page.get_text = lambda *args: []
    path = tmp_path / "output.pdf"
    path.write_bytes(b"%PDF-small fake")
    monkeypatch.setattr(renderer.fitz, "open", lambda *args: FakeDocument([page]))
    metadata = renderer._log_output_metadata(path, job_id=None)
    assert metadata is not None
    assert metadata["pages"][0]["raster_dimensions_estimated"] is True
    assert page.calls == 0


@pytest.mark.parametrize("existing", [False, True])
def test_late_rejection_never_publishes_or_deletes_preexisting_output(tmp_path, monkeypatch, existing):
    from app.core.ocr_v2.renderers import searchable_pdf as renderer
    target = tmp_path / "target.pdf"
    if existing:
        target.write_bytes(b"unrelated existing output")
    monkeypatch.setattr(renderer, "require_profile", lambda result, profile: result)
    monkeypatch.setattr(renderer.fitz, "open", lambda *args: FakeDocument())
    monkeypatch.setattr(renderer, "_log_output_metadata", lambda *args, **kw: None)
    def reject(*args, **kwargs):
        raise RasterAdmissionError("raster denied")
    monkeypatch.setattr(renderer, "validate_searchable_pdf_artifact", reject)
    with pytest.raises(RasterAdmissionError):
        renderer.SearchablePdfRenderer().render(tmp_path / "source.pdf", SimpleNamespace(pages=()), target)
    assert target.read_bytes() == b"unrelated existing output" if existing else not target.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == (["target.pdf"] if existing else [])


@pytest.mark.parametrize("width,height,dpi,expected", [
    (595.276, 841.89, 200, (1656, 2341)),
    (612, 792, 200, (1702, 2202)),
    (595.276, 841.89, 300, (2483, 3510)),
    # Binary matrix scale puts 792*scale just above 3300; round outwards.
    (612, 792, 300, (2552, 3303)),
])
def test_standard_pages_use_conservative_bounds(width, height, dpi, expected):
    result = estimate_raster(NeverRenderPage((0, 0, width, height)), dpi=dpi)
    assert (result.width, result.height) == expected


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_visible_rotated_cropbox_is_not_rotated_twice(rotation):
    # Page.rect already incorporates CropBox and rotation; CropBox origin
    # remains in the unrotated PDF coordinate system and must not be added.
    bounds = (0, 0, 200, 100) if rotation in (90, 270) else (0, 0, 100, 200)
    page = NeverRenderPage(bounds, rotation)
    page.cropbox = SimpleNamespace(x0=31, y0=42, x1=131, y1=242)
    result = estimate_raster(page, dpi=72)
    assert (result.width, result.height) == ((202, 102) if rotation in (90, 270) else (102, 202))


def test_fractional_non_origin_rect_and_visible_clip():
    import pymupdf as fitz
    page = NeverRenderPage((10.2, 20.1, 110.3, 220.4))
    result = estimate_raster(page, dpi=72, clip=fitz.Rect(20.2, 30.5, 70.9, 80.1))
    assert (result.width, result.height) == (53, 53)
    with pytest.raises(RasterAdmissionError):
        estimate_raster(page, clip=fitz.Rect(200, 200, 250, 250))


@pytest.mark.parametrize("budget,admitted", [(9999, False), (10000, True), (10001, True)])
def test_boundary_uses_conservative_integer_area(budget, admitted):
    page = NeverRenderPage((0, 0, 98, 98))  # outward guard makes 100x100
    if admitted:
        decision = estimate_raster(page, dpi=72, max_raster_pixels=budget)
        assert (decision.width, decision.height) == (100, 100)
    else:
        with pytest.raises(RasterAdmissionError):
            estimate_raster(page, dpi=72, max_raster_pixels=budget)
    assert page.calls == 0


def test_hard_ceiling_cannot_be_raised_and_missing_limit_is_safe():
    for limit in (None, 100_000_000):
        decision = estimate_raster(NeverRenderPage((0, 0, 2498, 3998)), dpi=72, max_raster_pixels=limit)
        assert (decision.width, decision.height) == (2500, 4000)
        with pytest.raises(RasterAdmissionError):
            estimate_raster(NeverRenderPage((0, 0, 2499, 3998)), dpi=72, max_raster_pixels=limit)


@pytest.mark.parametrize("bounds", [
    (0, 0, 0, 10), (0, 0, -1, 10), (2, 0, 1, 10),
    (0, 0, float("nan"), 10), (float("inf"), 0, 10, 10),
    (0, 0, 1e300, 1e300), (0, 0, 32767, 1), (0, 0, 1, 32767),
    (-1e100, 0, 1, 1),
])
def test_invalid_and_extreme_geometry_is_rejected_without_render(bounds):
    page = NeverRenderPage(bounds)
    with pytest.raises(RasterAdmissionError):
        render_page_pixmap(page, dpi=72)
    assert page.calls == 0


@pytest.mark.parametrize("matrix", [(), (True, 0, 0, True, 0, 0), (1, 0, 0, 0, 0, 0), (float("nan"), 0, 0, 1, 0, 0), (1000, 0, 0, 1000, 0, 0)])
def test_invalid_or_dpi_bypassing_transform_is_rejected(matrix):
    with pytest.raises(RasterAdmissionError):
        estimate_raster(NeverRenderPage((0, 0, 10, 10)), dpi=72, matrix=matrix)


@pytest.mark.parametrize("when", ["before", "after"])
def test_cancel_brackets_native_render_before_encoding(when, monkeypatch):
    from app.core.ocr_v2 import geometry
    calls = []
    page = NeverRenderPage((0, 0, 10, 10))
    def render(**kw):
        calls.append("render")
        return SimpleNamespace(width=10, height=10, tobytes=lambda *args: calls.append("encoded"))
    page.get_pixmap = render
    def cancel():
        if when == "before" or calls:
            raise OCRCancellationError("cancelled")
    with raster_policy(None, cancel), pytest.raises(OCRCancellationError):
        RasterPreparer(72).prepare(page)
    assert calls == ([] if when == "before" else ["render"])


def test_actual_pixmap_is_checked_before_encoding_or_pil(monkeypatch):
    from app.core.ocr_v2 import geometry
    page = NeverRenderPage((0, 0, 10, 10))
    calls = []
    page.get_pixmap = lambda **kw: SimpleNamespace(width=32769, height=1, tobytes=lambda *args: calls.append("encoded"))
    monkeypatch.setattr(geometry.Image, "open", lambda *args: calls.append("PIL"))
    with pytest.raises(RasterAdmissionError):
        RasterPreparer(72).prepare(page)
    assert calls == []


@pytest.mark.parametrize("unsafe", ["source", "output"])
def test_visual_validation_admits_both_pages_before_either_render(monkeypatch, unsafe):
    from app.core.ocr_v2.renderers import validation
    pages = [NeverRenderPage((0, 0, 100, 100)), NeverRenderPage((0, 0, 100, 100))]
    page = pages[0 if unsafe == "source" else 1]
    # Keep physical dimensions within the existing 0.01 comparison tolerance,
    # while only one conservative raster crosses a smaller configured cap.
    page.rect.x1 = page.rect.width = 100.001
    for item in pages:
        item.get_images = lambda **kw: [(1,)]
        item.get_image_info = lambda: []
        item.get_text = lambda *args: []
    docs = iter(FakeDocument([item]) for item in pages)
    monkeypatch.setattr(validation.fitz, "open", lambda *args: next(docs))
    result = SimpleNamespace(pages=[SimpleNamespace(tokens=())])
    with pytest.raises(RasterAdmissionError):
        validation.validate_searchable_pdf_artifact("source", "output", result, max_raster_pixels=10404)
    assert [page.calls for page in pages] == [0, 0]


def ocr_page(bounds=(0, 0, 100_000, 100_000)):
    page = NeverRenderPage(bounds)
    page.get_text = lambda kind: "" if kind == "text" else []
    page.get_images = lambda **kw: [(1,)]
    page.get_image_rects = lambda image: []
    return page


def forbid_allocations(monkeypatch):
    from app.core.ocr_v2 import geometry, orchestration
    from app.core.ocr_v2.adapters import TesseractAdapter, PPOCRv6MediumAdapter
    from app.core.ocr_v2.routing import OCRRouter
    calls = []
    def forbidden(*args, **kw):
        calls.append("allocation/probe/recognition")
        raise AssertionError("post-admission operation began")
    monkeypatch.setattr(OCRRouter, "_available", lambda *args: True)
    monkeypatch.setattr(geometry.Image, "open", forbidden)
    monkeypatch.setattr(geometry.Image.Image, "convert", forbidden)
    monkeypatch.setattr(orchestration, "_language_detection_sample", forbidden)
    monkeypatch.setattr(TesseractAdapter, "probe", forbidden)
    monkeypatch.setattr(TesseractAdapter, "recognize_page", forbidden)
    monkeypatch.setattr(PPOCRv6MediumAdapter, "recognize_page", forbidden)
    return calls


@pytest.mark.parametrize("profile", ["OCR_TEXT_V2", "SEARCHABLE_PDF_V2", "DOCUMENT_EXTRACTION_V2"])
def test_policy_aborts_multipage_worker_after_completed_page(monkeypatch, profile):
    from app.core.ocr_v2 import orchestration
    from app.core.ocr_v2.validation import OCRProfile
    safe = ocr_page((0, 0, 100, 100))
    safe.get_images = lambda **kw: []
    safe.get_text = lambda kind: "Safe document" if kind == "text" else [(10, 10, 50, 20, "Safe", 0, 0)]
    rejected = ocr_page()
    untouched = ocr_page()
    calls = forbid_allocations(monkeypatch)
    monkeypatch.setattr(orchestration.fitz, "open", lambda *args: FakeDocument([safe, rejected, untouched]))
    progress = []
    with pytest.raises(RasterAdmissionError):
        orchestration.OCRV2Worker().process_document("fake.pdf", language="auto", profile=OCRProfile(profile), page_progress_callback=lambda done, total, page: progress.append(done))
    assert progress == [1]
    assert [rejected.calls, untouched.calls] == [0, 0]
    assert calls == []


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("dpi", [72, 144, 200, 300, 600])
def test_small_native_crop_rotation_bounds_and_pixels(rotation, dpi):
    import pymupdf as fitz
    with fitz.open() as document:
        page = document.new_page(width=120, height=90)
        page.insert_text((15, 40), "Crop test", fontsize=8)
        page.set_cropbox(fitz.Rect(10.25, 15.5, 100.75, 80.25))
        page.set_rotation(rotation)
        clip = fitz.Rect(3.25, 4.5, 40.75, 50.125)
        admission = estimate_raster(page, dpi=dpi, clip=clip)
        pixmap = render_page_pixmap(page, dpi=dpi, clip=clip)
        reference = page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), clip=clip, alpha=False)
        assert (pixmap.width, pixmap.height) == (reference.width, reference.height)
        assert pixmap.width <= admission.width and pixmap.height <= admission.height
        assert pixmap.samples_mv == reference.samples_mv


def test_secondary_metadata_does_not_swallow_policy_error(tmp_path, monkeypatch):
    from app.core.ocr_v2.renderers import searchable_pdf as renderer
    path = tmp_path / "output.pdf"
    path.write_bytes(b"%PDF-fake")
    page = NeverRenderPage()
    monkeypatch.setattr(renderer.fitz, "open", lambda *args: FakeDocument([page]))
    with pytest.raises(RasterAdmissionError):
        renderer._log_output_metadata(path, job_id=None)
    assert page.calls == 0


@pytest.mark.parametrize("rotation", [True, "90", 45, 360, float("nan")])
def test_invalid_rotation_cannot_reach_renderer(rotation):
    page = NeverRenderPage((0, 0, 10, 10), rotation=rotation)
    with pytest.raises(RasterAdmissionError):
        render_page_pixmap(page)
    assert page.calls == 0


def test_nested_policy_restores_limit_after_failure():
    page = NeverRenderPage((0, 0, 20, 20))
    with raster_policy(100), pytest.raises(RasterAdmissionError):
        estimate_raster(page, dpi=72)
    assert estimate_raster(page, dpi=72).width == 22


def test_missing_geometry_is_a_fatal_admission_error():
    with pytest.raises(RasterAdmissionError):
        estimate_raster(SimpleNamespace(rotation=0))


def test_failed_ocr_page_cannot_become_structured_blank_or_success(tmp_path, monkeypatch):
    from app.core.ocr_v2 import structured
    from app.core.ocr_v2.contracts import PageResult, PageGeometry, PageStatus, PageContentClassification, PageProcessingSource
    page = PageResult(0, "page-0", PageGeometry(100, 100),
                      PageContentClassification.IMAGE_SCAN, PageProcessingSource.NONE, PageStatus.FAILED, "")
    worker = SimpleNamespace(process_document=lambda *a, **kw: SimpleNamespace(pages=(page,)))
    monkeypatch.setattr(structured, "OCRV2Worker", lambda **kw: worker)
    monkeypatch.setattr(structured.fitz, "open", lambda *args: FakeDocument([object()]))
    monkeypatch.setattr(structured, "calculate_document_base_font_size", lambda *a: 10)
    monkeypatch.setattr(structured, "_native_elements", lambda *a: [])
    import pdfplumber
    def no_plumber(*args):
        raise ValueError("fake document")
    monkeypatch.setattr(pdfplumber, "open", no_plumber)
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"%PDF-fake")
    result = structured.StructuredDocumentProcessor().process_document(path)
    assert result.pages[0].status == "FAILED"
    assert result.validation["valid"] is False
    assert result.validation["issues"][0]["code"] == "OCR_PAGE_FAILED"


def test_axis_boundary_is_admitted_without_rendering():
    result = estimate_raster(NeverRenderPage((0, 0, 32766, 1)), dpi=72)
    assert (result.width, result.height) == (32768, 3)


def test_renderer_second_page_admission_preserves_destination_and_staging(tmp_path, monkeypatch):
    from app.core.ocr_v2.renderers import searchable_pdf as renderer
    source = NeverRenderPage((0, 0, 100, 100))
    output = NeverRenderPage((0, 0, 100.001, 100))
    for page in (source, output):
        page.get_images = lambda **kw: [(1,)]
        page.get_image_info = lambda: []
        page.get_text = lambda *args: []
    docs = iter([FakeDocument([source]), FakeDocument([source]), FakeDocument([output])])
    monkeypatch.setattr(renderer.fitz, "open", lambda *args: next(docs))
    monkeypatch.setattr(renderer, "require_profile", lambda result, profile: result)
    monkeypatch.setattr(renderer, "_log_output_metadata", lambda *args, **kw: None)
    target = tmp_path / "target.pdf"
    target.write_bytes(b"existing unrelated destination")
    result = SimpleNamespace(pages=[SimpleNamespace(page_index=0, tokens=(), reading_order=())])
    with pytest.raises(RasterAdmissionError):
        renderer.SearchablePdfRenderer(max_raster_pixels=10404).render("source.pdf", result, target)
    assert [source.calls, output.calls] == [0, 0]
    assert target.read_bytes() == b"existing unrelated destination"
    assert [path.name for path in tmp_path.iterdir()] == ["target.pdf"]

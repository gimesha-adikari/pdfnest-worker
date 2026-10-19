"""F-11 hostile geometry uses fakes and never allocates a native raster."""
from types import SimpleNamespace

import pytest

from app.core.ocr_v2.geometry import RasterPreparer
from app.core.ocr_v2.errors import RasterAdmissionError


class NeverRenderPage:
    rotation = 0
    number = 0

    def __init__(self, width=100_000, height=100_000):
        self.rect = SimpleNamespace(x0=0, y0=0, x1=width, y1=height, width=width, height=height)
        self.calls = 0

    def get_pixmap(self, **kwargs):
        self.calls += 1
        raise AssertionError("unsafe native rasterization began")


def test_default_preparer_rejects_before_native_allocation():
    page = NeverRenderPage()
    with pytest.raises(ValueError, match="raster"):
        RasterPreparer().prepare(page)
    assert page.calls == 0


class FakeDocument:
    needs_pass = False
    is_encrypted = False

    def __init__(self, pages):
        self.pages = pages

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


@pytest.mark.parametrize("entry", ["ocr", "preview", "editor", "compile", "markdown"])
def test_legacy_rasters_reject_before_native_call(entry, tmp_path, monkeypatch):
    from app.api.tools.ocr import document as ocr
    from app.api.tools.editor import document as editor
    from app.api.tools.markdown import service as markdown
    page = NeverRenderPage()
    page.get_text = lambda *args: []
    document = FakeDocument([page])
    monkeypatch.setattr(editor.fitz, "open", lambda *args: document)
    monkeypatch.setattr(markdown.fitz, "open", lambda *args: document)
    monkeypatch.setattr(markdown, "validate_ocr_language", lambda *args: None)
    monkeypatch.setattr(markdown, "calculate_document_base_font_size", lambda *args: 10)
    monkeypatch.setattr(markdown, "classify_page_content", lambda *args: "SCANNED")
    monkeypatch.setattr(markdown, "extract_page_links", lambda *args: [])
    monkeypatch.setattr(markdown, "is_table_candidate_page", lambda *args: False)
    def no_plumber(*args, **kwargs):
        raise ValueError("fake input")
    monkeypatch.setattr(markdown.pdfplumber, "open", no_plumber)
    payload = tmp_path / "layout.json"
    payload.write_text('{"pages":[{"kind":"scanned","elements":[]}]}')
    with pytest.raises(RasterAdmissionError):
        if entry == "ocr":
            ocr._render_page_image(page)
        elif entry == "preview":
            ocr._render_page_preview(page)
        elif entry == "editor":
            editor.ocr_words_for_page(page)
        elif entry == "compile":
            editor.compile_document("source.pdf", str(tmp_path / "out.pdf"), str(payload))
        else:
            markdown.convert_pdf_to_markdown("fake.pdf")
    assert page.calls == 0
    assert not (tmp_path / "out.pdf").exists()


def test_word_preflight_cannot_approve_guard_rejected_dpi():
    from app.api.tools.pdf_to_office.converters.word import _max_safe_page_raster_dpi
    from app.core.ocr_v2.geometry import estimate_raster
    page = NeverRenderPage(2499, 3998)
    dpi = _max_safe_page_raster_dpi(page)
    assert dpi == 71
    estimate_raster(page, dpi=dpi)
    assert page.calls == 0


def test_word_preflight_preserves_smaller_structured_budget(monkeypatch):
    from app.api.tools.pdf_to_office.converters.word import _max_safe_page_raster_dpi
    from app.core.ocr_v2.geometry import estimate_raster
    monkeypatch.setenv("OCR_V2_STRUCTURED_MAX_RASTER_PIXELS", "100")
    page = NeverRenderPage(20, 20)
    dpi = _max_safe_page_raster_dpi(page)
    assert dpi == 28
    estimate_raster(page, dpi=dpi, max_raster_pixels=100)


def test_word_preflight_rejects_when_minimum_dpi_cannot_fit():
    from app.api.tools.pdf_to_office.converters.word import _max_safe_page_raster_dpi
    page = NeverRenderPage(1_000_000, 1_000_000)
    with pytest.raises(RasterAdmissionError):
        _max_safe_page_raster_dpi(page)
    assert page.calls == 0


def test_editor_clipped_background_rejects_extreme_coordinates():
    import pymupdf as fitz
    from app.api.tools.editor.document import sample_background_hex
    page = NeverRenderPage()
    page.rect = fitz.Rect(0, 0, 2_000_000, 2_000_000)
    with pytest.raises(RasterAdmissionError):
        sample_background_hex(page, fitz.Rect(10, 10, 20, 20))
    assert page.calls == 0


def test_page_scoped_preparer_keeps_selected_dpi_and_smaller_budget():
    from app.core.pdf_to_word_ocr_engine import _PageScopedRasterPreparer
    from app.core.ocr_v2.geometry import raster_policy
    # At 72 DPI the conservative 100x100 fits; at 200 it would not.
    page = NeverRenderPage(98, 98)
    scoped = _PageScopedRasterPreparer((72,), RasterPreparer)
    with raster_policy(9999), pytest.raises(RasterAdmissionError):
        scoped.prepare(page)
    assert scoped._preparers[72].dpi == 72
    assert page.calls == 0


@pytest.mark.parametrize("dpi", [True, 0, -1, 601, 1.5, float("nan"), float("inf")])
def test_invalid_dpi_has_typed_configuration_error(dpi):
    from app.core.ocr_v2.errors import ConfigurationError
    with pytest.raises(ConfigurationError):
        RasterPreparer(dpi)


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, float("nan"), float("inf")])
def test_invalid_limit_has_typed_configuration_error(limit):
    from app.core.ocr_v2.errors import ConfigurationError
    from app.core.ocr_v2.orchestration import OCRV2Worker
    with pytest.raises(ConfigurationError):
        OCRV2Worker(max_raster_pixels=limit)

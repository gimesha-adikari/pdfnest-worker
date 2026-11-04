from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from pptx import Presentation

from app.api.tools.pdf_to_office.converters import powerpoint
from app.core.ocr_v2.errors import RasterAdmissionError


class _OversizedPage:
    rect = fitz.Rect(0, 0, 4_000, 4_000)
    rotation = 0

    def get_pixmap(self, **_kwargs):
        raise AssertionError("native raster allocation reached before geometry admission")


class _Document:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def __len__(self):
        return 1

    def load_page(self, _number):
        return self.page

    def close(self):
        self.closed = True


def test_powerpoint_rejects_oversized_page_before_get_pixmap(monkeypatch, tmp_path: Path):
    document = _Document(_OversizedPage())
    monkeypatch.setattr(powerpoint.fitz, "open", lambda _path: document)

    with pytest.raises(RasterAdmissionError, match="raster dimensions exceed"):
        powerpoint.convert_to_powerpoint("synthetic.pdf", str(tmp_path / "out.pptx"))

    assert document.closed
    assert not (tmp_path / "out.pptx").exists()


def test_powerpoint_keeps_normal_page_geometry_and_page_order(tmp_path: Path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pptx"
    doc = fitz.open()
    for label in ("first slide", "second slide"):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), label)
    doc.save(source)
    doc.close()

    powerpoint.convert_to_powerpoint(str(source), str(output))

    presentation = Presentation(str(output))
    assert len(presentation.slides) == 2
    assert presentation.slide_width == pytest.approx(612 * 12_700)
    assert presentation.slide_height == pytest.approx(792 * 12_700)
    assert len(presentation.slides[0].shapes) == 1
    assert len(presentation.slides[1].shapes) == 1

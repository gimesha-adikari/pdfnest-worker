from __future__ import annotations

import math
from numbers import Real
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from PIL import Image


from app.core.ocr_v2.geometry import estimate_raster, MAX_RASTER_DPI


JPEG_QUALITY = 85


def validate_preview_dpi(dpi: float) -> None:
    if isinstance(dpi, bool) or not isinstance(dpi, Real) or not math.isfinite(dpi) or not 0 < dpi <= MAX_RASTER_DPI:
        raise ValueError("DPI must be finite, positive and at most 600")


def encode_pixmap_to_jpeg(pixmap: fitz.Pixmap) -> bytes:
    image = Image.frombytes(
        "RGB",
        [pixmap.width, pixmap.height],
        pixmap.samples,
    )

    if image.mode != "RGB":
        image = image.convert("RGB")

    output = BytesIO()

    image.save(
        output,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
    )

    return output.getvalue()


def render_page_from_document(
        document: fitz.Document,
        page_number: int,
        dpi: float,
        clip_x0: float | None = None,
        clip_y0: float | None = None,
        clip_x1: float | None = None,
        clip_y1: float | None = None,
) -> bytes:
    if page_number < 1 or page_number > document.page_count:
        raise ValueError(
            f"Invalid page {page_number}; "
            f"document has {document.page_count} pages"
        )

    validate_preview_dpi(dpi)

    page = document[page_number - 1]

    zoom = float(dpi) / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    clips = (clip_x0, clip_y0, clip_x1, clip_y1)
    clip_rect = None
    if any(value is not None for value in clips):
        if any(value is None or not math.isfinite(value) for value in clips):
            raise ValueError("Clip must contain four finite coordinates")
        if clip_x1 <= clip_x0 or clip_y1 <= clip_y0:
            raise ValueError("Clip must have positive dimensions")
        clip_rect = fitz.Rect(*clips)

    # The OCR guard intentionally accepts integer DPI. Round upward only for
    # admission, preserving fractional preview resolution at the native sink.
    admission = estimate_raster(page, dpi=math.ceil(dpi), clip=clip_rect)
    pixmap = page.get_pixmap(
        matrix=matrix, clip=admission.clip, alpha=False, colorspace=fitz.csRGB,
    )
    if pixmap.width > admission.width or pixmap.height > admission.height:
        raise ValueError("Native raster exceeds admitted dimensions")

    return encode_pixmap_to_jpeg(pixmap)


class PdfRenderDocument:
    def __init__(
            self,
            document: fitz.Document,
    ) -> None:
        self.document = document

    @classmethod
    def open(
            cls,
            file_path: Path,
    ) -> "PdfRenderDocument":
        document = fitz.open(str(file_path))

        if document.page_count <= 0:
            document.close()
            raise ValueError("PDF contains no pages")

        return cls(document)

    @property
    def page_count(self) -> int:
        return self.document.page_count

    def render_page(
            self,
            page_number: int,
            dpi: float,
            clip_x0: float | None = None,
            clip_y0: float | None = None,
            clip_x1: float | None = None,
            clip_y1: float | None = None,
    ) -> bytes:
        return render_page_from_document(
            document=self.document,
            page_number=page_number,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
        )

    def close(self) -> None:
        try:
            self.document.close()
        except Exception:
            pass


def render_pdf_page_to_jpeg(
        pdf_bytes: bytes,
        page_number: int,
        dpi: float,
        clip_x0: float | None = None,
        clip_y0: float | None = None,
        clip_x1: float | None = None,
        clip_y1: float | None = None,
) -> bytes:
    if not pdf_bytes:
        raise ValueError("Empty file uploaded")

    if page_number < 1:
        raise ValueError("Page number must be 1 or greater")

    validate_preview_dpi(dpi)

    document = fitz.open(
        stream=pdf_bytes,
        filetype="pdf",
    )

    try:
        return render_page_from_document(
            document=document,
            page_number=page_number,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
        )
    finally:
        document.close()
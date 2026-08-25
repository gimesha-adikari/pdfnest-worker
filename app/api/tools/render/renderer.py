from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from PIL import Image


JPEG_QUALITY = 85


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

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

    page = document[page_number - 1]

    zoom = float(dpi) / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    clip_rect = None
    if clip_x0 is not None and clip_y0 is not None and clip_x1 is not None and clip_y1 is not None:
        if clip_x1 > clip_x0 and clip_y1 > clip_y0:
            clip_rect = fitz.Rect(clip_x0, clip_y0, clip_x1, clip_y1)

    pixmap = page.get_pixmap(
        matrix=matrix,
        clip=clip_rect,
        alpha=False,
    )

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

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

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
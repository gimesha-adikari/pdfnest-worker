from __future__ import annotations

from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image


def render_pdf_page_to_jpeg(pdf_bytes: bytes, page_number: int, dpi: float) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(
                f"Invalid page {page_number}; document has {doc.page_count} pages"
            )

        page = doc[page_number - 1]
        zoom = float(dpi) / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        if img.mode != "RGB":
            img = img.convert("RGB")

        out = BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()
    finally:
        doc.close()
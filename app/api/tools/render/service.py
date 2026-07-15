from __future__ import annotations

from fastapi import UploadFile

from .renderer import render_pdf_page_to_jpeg


async def render_page_to_jpeg_bytes(file: UploadFile, page: int, dpi: float) -> bytes:
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise ValueError("Empty file uploaded")

    if page < 1:
        raise ValueError("Page number must be 1 or greater")

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

    return render_pdf_page_to_jpeg(pdf_bytes=pdf_bytes, page_number=page, dpi=dpi)
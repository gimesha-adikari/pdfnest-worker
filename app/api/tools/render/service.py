from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import UploadFile

from .renderer import PdfRenderDocument, render_pdf_page_to_jpeg
from .session import RenderSession, session_manager, sha256_file


async def render_page_to_jpeg_bytes(
        file: UploadFile,
        page: int,
        dpi: float,
) -> bytes:
    if page < 1:
        raise ValueError("Page number must be 1 or greater")

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

    pdf_bytes = await file.read()

    if not pdf_bytes:
        raise ValueError("Empty file uploaded")

    return render_pdf_page_to_jpeg(
        pdf_bytes=pdf_bytes,
        page_number=page,
        dpi=dpi,
    )


async def create_render_session(
        file: UploadFile,
) -> RenderSession:
    temp_file = tempfile.NamedTemporaryFile(
        prefix="pdfnest-session-upload-",
        suffix=".pdf",
        delete=False,
    )

    temp_path = Path(temp_file.name)

    try:
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            temp_file.write(chunk)

        temp_file.flush()
        temp_file.close()

        file_size = temp_path.stat().st_size

        if file_size <= 0:
            raise ValueError("Empty file uploaded")

        sha256 = sha256_file(temp_path)

        existing = session_manager.find_by_hash(sha256)

        if existing is not None:
            return existing

        return session_manager.create(
            file_path=temp_path,
            file_size=file_size,
            sha256=sha256,
        )

    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def get_render_session_page(
        session_id: str,
        page: int,
        dpi: float,
) -> bytes:
    if page < 1:
        raise ValueError("Page number must be 1 or greater")

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

    session = session_manager.get(session_id)

    cache_key = f"{page}:{dpi:g}"

    with session.lock:
        cached = session.page_cache.get(cache_key)

        if cached is not None:
            return cached

        if session.document is None:
            session.document = PdfRenderDocument.open(
                session.file_path,
            )

        image_bytes = session.document.render_page(
            page_number=page,
            dpi=dpi,
        )

        session.page_cache[cache_key] = image_bytes

        return image_bytes


def delete_render_session(
        session_id: str,
) -> bool:
    session = session_manager.get_optional(session_id)

    if session is None:
        return False

    session_manager.delete(session_id)
    return True
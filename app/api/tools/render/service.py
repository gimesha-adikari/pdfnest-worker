import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import UploadFile

from app.core.config import settings
from .persistent_pool import (
    PersistentWorkerInfrastructureError,
    PersistentWorkerTimeoutError,
    get_persistent_render_pool,
)
from .renderer import PdfRenderDocument, render_pdf_page_to_jpeg
from .session import RenderSession, session_manager, sha256_file


async def render_page_to_jpeg_bytes_subprocess(
        pdf_bytes: bytes,
        page: int,
        dpi: float,
        clip_x0: float | None = None,
        clip_y0: float | None = None,
        clip_x1: float | None = None,
        clip_y1: float | None = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Certified Phase 3F/3G isolated subprocess renderer.
    Renders a PDF page in a clean, disposable subprocess.
    """
    temp_in = tempfile.NamedTemporaryFile(prefix="pdfnest-render-in-", suffix=".pdf", delete=False)
    temp_out = tempfile.NamedTemporaryFile(prefix="pdfnest-render-out-", suffix=".jpg", delete=False)

    in_path = Path(temp_in.name)
    out_path = Path(temp_out.name)

    try:
        temp_in.write(pdf_bytes)
        temp_in.flush()
        temp_in.close()
        temp_out.close()

        cmd = [
            sys.executable,
            "-m",
            "app.api.tools.render.render_cli",
            "--input",
            str(in_path),
            "--output",
            str(out_path),
            "--page",
            str(page),
            "--dpi",
            str(dpi),
        ]
        if clip_x0 is not None:
            cmd.extend(["--clip-x0", str(clip_x0)])
        if clip_y0 is not None:
            cmd.extend(["--clip-y0", str(clip_y0)])
        if clip_x1 is not None:
            cmd.extend(["--clip-x1", str(clip_x1)])
        if clip_y1 is not None:
            cmd.extend(["--clip-y1", str(clip_y1)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            stdout, stderr = await proc.communicate()
        except BaseException:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            raise

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            if "Invalid page" in err_msg or "Page number must be" in err_msg or "DPI must be" in err_msg or "Empty file" in err_msg or "PDF contains no pages" in err_msg:
                raise ValueError(err_msg)
            raise RuntimeError(f"Render process failed with code {proc.returncode}: {err_msg}")

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("Render process did not generate output JPEG")

        child_metrics = {}
        if stdout:
            import json
            try:
                child_metrics = json.loads(stdout.decode().strip())
            except Exception:
                pass

        return out_path.read_bytes(), child_metrics

    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


async def render_page_to_jpeg_bytes(
        file: UploadFile,
        page: int,
        dpi: float,
        clip_x0: float | None = None,
        clip_y0: float | None = None,
        clip_x1: float | None = None,
        clip_y1: float | None = None,
        simulate_crash: bool = False,
        simulate_hang: bool = False,
) -> Tuple[bytes, Dict[str, Any]]:
    if page < 1:
        raise ValueError("Page number must be 1 or greater")

    if dpi <= 0:
        raise ValueError("DPI must be greater than 0")

    pdf_bytes = await file.read()

    if not pdf_bytes:
        raise ValueError("Empty file uploaded")

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("File is not a valid PDF")

    # 1. Check feature flag: default is False -> certified subprocess renderer
    if not settings.enable_persistent_render_pool:
        return await render_page_to_jpeg_bytes_subprocess(
            pdf_bytes=pdf_bytes,
            page=page,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
        )

    # 2. Persistent Pool path with automatic fallback on infrastructure failure
    pool = get_persistent_render_pool()
    if pool is None or not pool.is_running:
        return await render_page_to_jpeg_bytes_subprocess(
            pdf_bytes=pdf_bytes,
            page=page,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
        )

    try:
        return await pool.render(
            pdf_bytes=pdf_bytes,
            page=page,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
            simulate_crash=simulate_crash,
            simulate_hang=simulate_hang,
        )
    except ValueError:
        # Normal user/input error: do not retry fallback
        raise
    except (PersistentWorkerTimeoutError, PersistentWorkerInfrastructureError) as exc:
        # Infrastructure/worker failure: log, increment fallback count with granular reason, and fallback to certified subprocess renderer
        reason = "timeout" if isinstance(exc, PersistentWorkerTimeoutError) or "timeout" in str(exc).lower() else "crash"
        pool.increment_fallback_count(reason=reason)
        import logging
        logging.getLogger(__name__).warning(
            f"Persistent render pool {reason} error: {exc}. Falling back to certified subprocess renderer."
        )
        return await render_page_to_jpeg_bytes_subprocess(
            pdf_bytes=pdf_bytes,
            page=page,
            dpi=dpi,
            clip_x0=clip_x0,
            clip_y0=clip_y0,
            clip_x1=clip_x1,
            clip_y1=clip_y1,
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
import asyncio
import os
import functools
import subprocess
import threading
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, UploadFile

from app.core.config import settings
from .persistent_pool import (
    PersistentWorkerInfrastructureError,
    PersistentWorkerTimeoutError,
    get_persistent_render_pool,
)
from .renderer import validate_preview_dpi
from .session import RenderSession, session_manager, sha256_file
from .session_metadata import session_file_io
from .child_limits import MAX_RENDER_CHILD_FILE_BYTES


MAX_RENDER_CHILD_CAPTURE_BYTES = 64 * 1024
MAX_RENDER_INPUT_BYTES = 64 * 1024 * 1024
MAX_RENDER_OUTPUT_BYTES = MAX_RENDER_CHILD_FILE_BYTES


async def _read_render_upload(file: UploadFile) -> bytes:
    data = bytearray()
    while True:
        chunk = await file.read(min(1024 * 1024, MAX_RENDER_INPUT_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data)
        if len(data) + len(chunk) > MAX_RENDER_INPUT_BYTES:
            raise HTTPException(413, detail={
                "code": "RENDER_INPUT_TOO_LARGE",
                "message": "PDF exceeds the supported preview size.",
            })
        data.extend(chunk)


async def render_page_to_jpeg_bytes_subprocess(
        pdf_bytes: bytes,
        page: int,
        dpi: float,
        clip_x0: float | None = None,
        clip_y0: float | None = None,
        clip_x1: float | None = None,
        clip_y1: float | None = None,
        pdf_path: Path | None = None,
        storage_scope: str = "global",
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Certified Phase 3F/3G isolated subprocess renderer.
    Renders a PDF page in a clean, disposable subprocess.
    """
    from .session import SESSION_ROOT
    input_reservation = session_manager.reserve_storage(storage_scope)
    output_reservation = session_manager.reserve_storage(storage_scope)
    directory = None
    cancelled = threading.Event()

    try:
        validate_preview_dpi(dpi)
        # Input, output, sessions and failed removals consume the same atomic
        # budget. Reserve the maximum output before permitting native work.
        input_reservation.extend(len(pdf_bytes) if pdf_path is None else 0)
        output_reservation.extend(MAX_RENDER_OUTPUT_BYTES)
        directory = Path(tempfile.mkdtemp(prefix="pdfnest-render-job-", dir=SESSION_ROOT))
        in_path = directory / "input.pdf"
        out_path = directory / "output.jpg"
        if pdf_path is None:
            await session_file_io(in_path.write_bytes, pdf_bytes)
        out_path.touch(mode=0o600)

        cmd = [
            sys.executable,
            "-m",
            "app.api.tools.render.render_cli",
            "--input",
            str(pdf_path if pdf_path is not None else in_path),
            "--output",
            str(out_path),
            "--page",
            str(page),
            "--dpi",
            str(dpi),
            "--owner-pid",
            str(os.getpid()),
        ]
        if clip_x0 is not None:
            cmd.extend(["--clip-x0", str(clip_x0)])
        if clip_y0 is not None:
            cmd.extend(["--clip-y0", str(clip_y0)])
        if clip_x1 is not None:
            cmd.extend(["--clip-x1", str(clip_x1)])
        if clip_y1 is not None:
            cmd.extend(["--clip-y1", str(clip_y1)])

        from app.core.subprocess_runner import run_hardened_subprocess
        from .session import _SESSION_ROOT_LOCK
        from .limiter import render_limiter

        def check_cancelled():
            if cancelled.is_set():
                raise RuntimeError("preview operation cancelled")

        operation = functools.partial(
            run_hardened_subprocess, cmd,
            timeout=render_limiter.render_timeout,
            cancellation_check=check_cancelled,
            max_output_bytes=MAX_RENDER_CHILD_CAPTURE_BYTES,
            truncate_output=True,
            preview_root_fd=_SESSION_ROOT_LOCK.fileno(),
            preview_drain_marker=str(directory / "writer.drained"),
        )
        try:
            result = await session_file_io(operation, cancelled=cancelled)
        except subprocess.TimeoutExpired as exc:
            raise asyncio.TimeoutError() from exc

        truncated = result.capture_truncated
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            if truncated["stderr"]:
                err_msg += " [child stderr truncated]"
            if result.returncode == 3 or any(text in err_msg for text in (
                "Invalid page", "Page number must be", "DPI must be", "Empty file", "PDF contains no pages",
            )):
                raise ValueError(err_msg)
            raise RuntimeError(f"Render process failed with code {result.returncode}: {err_msg}")

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("Render process did not generate output JPEG")

        # Check the intermediate file before allocating it in the API. Retain
        # a bounded read too, so a changing file cannot bypass the stat check.
        if out_path.stat().st_size > MAX_RENDER_OUTPUT_BYTES:
            raise RuntimeError("Rendered output exceeds the preview byte limit")
        with out_path.open("rb") as rendered:
            image_bytes = rendered.read(MAX_RENDER_OUTPUT_BYTES + 1)
        if len(image_bytes) > MAX_RENDER_OUTPUT_BYTES:
            raise RuntimeError("Rendered output exceeds the preview byte limit")

        child_metrics = {}
        if result.stdout and not truncated["stdout"]:
            import json
            try:
                child_metrics = json.loads(result.stdout.strip())
            except Exception:
                pass

        return image_bytes, child_metrics

    finally:
        if directory is None:
            input_reservation.release()
            output_reservation.release()
        else:
            session_manager.cleanup_render_staging(in_path, input_reservation)
            session_manager.cleanup_render_staging(out_path, output_reservation)
            session_manager.cleanup_render_directory(directory)


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

    validate_preview_dpi(dpi)

    pdf_bytes = await _read_render_upload(file)

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
        dedup_scope: str = "global",
) -> RenderSession:
    from .limiter import render_limiter
    operation = _create_render_session(file, dedup_scope)
    try:
        return await render_limiter.run(operation)
    finally:
        # Admission may reject before the coroutine is ever awaited.
        operation.close()


async def _create_render_session(
        file: UploadFile,
        dedup_scope: str = "global",
) -> RenderSession:
    from .session import SESSION_ROOT
    reservation = session_manager.reserve_storage(dedup_scope)
    temp_file = None
    temp_path: Path | None = None

    try:
        SESSION_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            prefix="pdfnest-session-upload-",
            suffix=".pdf",
            dir=SESSION_ROOT,
            delete=False,
        )
        temp_path = Path(temp_file.name)
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            # Reserve before writing so concurrent uploads cannot collectively
            # exceed the aggregate or owner byte budget.
            reservation.extend(len(chunk))
            await session_file_io(temp_file.write, chunk)

        temp_file.flush()
        temp_file.close()

        file_size = temp_path.stat().st_size

        if file_size <= 0:
            raise ValueError("Empty file uploaded")

        sha256 = await session_file_io(sha256_file, temp_path)

        return await session_manager.get_or_create(
            file_path=temp_path,
            file_size=file_size,
            sha256=sha256,
            dedup_scope=dedup_scope,
            reservation=reservation,
        )

    finally:
        try:
            if temp_file is not None:
                temp_file.close()
        finally:
            session_manager.cleanup_upload_staging(
                temp_path,
                reservation,
                scope=dedup_scope,
            )


async def get_render_session_page(
        session_id: str,
        page: int,
        dpi: float,
) -> bytes:
    if page < 1:
        raise ValueError("Page number must be 1 or greater")

    validate_preview_dpi(dpi)

    # Keep source liability until the child is reaped. Logical deletion makes
    # the session inaccessible immediately; physical cleanup waits for readers.
    with session_manager.render_source(session_id) as session:
        image_bytes, _ = await render_page_to_jpeg_bytes_subprocess(
            pdf_bytes=b"", pdf_path=session.file_path, page=page, dpi=dpi,
            storage_scope=session.dedup_scope,
        )
    return image_bytes


def delete_render_session(
        session_id: str,
) -> bool:
    session = session_manager.get_optional(session_id)

    if session is None:
        return False

    session_manager.delete(session_id)
    return True

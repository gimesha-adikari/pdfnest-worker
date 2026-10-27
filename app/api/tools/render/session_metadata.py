"""Bounded session metadata inspection; the API never opens the native PDF."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

METADATA_TIMEOUT_SECONDS = 30.0
_MODULE = "app.api.tools.render.session_metadata"


class MetadataBusy(Exception):
    pass


class MetadataTimeout(Exception):
    pass


class MetadataFailed(Exception):
    pass


class MetadataUnavailable(Exception):
    pass


class _MetadataCancelled(Exception):
    pass


async def session_file_io(function, *args, cancelled=None):
    """Join bounded admitted work before its caller removes input files.

    Native work receives an explicit cancellation event; regular file I/O must
    finish before descriptors/paths can be closed. Repeated task cancellation
    must not detach the worker thread or race its cleanup.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancelled is not None:
            cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def _inspect(path: Path, cancelled: threading.Event) -> int:
    from app.core.native_isolation import _capacity
    from app.core.ocr_v2.errors import OCRTimeoutError
    from app.core.subprocess_runner import run_hardened_subprocess

    def check():
        if cancelled.is_set():
            raise _MetadataCancelled()

    check()
    try:
        # Reuse the existing local native slot, without accumulating waiting
        # threads behind an unrelated OCR job. API admission is bounded too.
        with _capacity(check, 0), tempfile.TemporaryDirectory(prefix="pdfnest-session-metadata-") as scratch:
            env = os.environ.copy()
            env.update(TMPDIR=scratch, TMP=scratch, TEMP=scratch,
                       OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4])
            result = run_hardened_subprocess(
                [sys.executable, "-m", _MODULE, str(path), str(os.getpid())],
                timeout=METADATA_TIMEOUT_SECONDS,
                cancellation_check=check, env=env,
                max_output_bytes=1024, cleanup_group_on_exit=True,
            )
    except OCRTimeoutError as exc:
        raise MetadataBusy() from exc
    except subprocess.TimeoutExpired as exc:
        raise MetadataTimeout() from exc
    except OSError as exc:
        raise MetadataUnavailable() from exc
    except RuntimeError as exc:
        raise MetadataFailed() from exc
    if result.returncode != 0:
        raise MetadataFailed()
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise MetadataFailed() from exc
    if response == {"error": "INVALID_PDF"}:
        raise ValueError("File is not a valid PDF")
    if response == {"error": "EMPTY_PDF"}:
        raise ValueError("PDF contains no pages")
    if response == {"error": "UNAVAILABLE"}:
        raise MetadataUnavailable()
    if not isinstance(response, dict) or set(response) != {"page_count"}:
        raise MetadataFailed()
    count = response["page_count"]
    if type(count) is not int or count <= 0:
        raise MetadataFailed()
    check()
    return count


async def inspect_page_count(path: Path) -> int:
    cancelled = threading.Event()
    return await session_file_io(_inspect, path, cancelled, cancelled=cancelled)


def _child(path: str) -> None:
    from app.core.native_child import apply_resource_limits
    apply_resource_limits()
    # Only the resource-limited disposable child imports/uses native parsing.
    import pymupdf as fitz
    from .renderer import PdfRenderDocument
    try:
        document = PdfRenderDocument.open(Path(path))
        try:
            response = {"page_count": document.page_count}
        finally:
            document.close()
    except ValueError as exc:
        response = {"error": "EMPTY_PDF" if str(exc) == "PDF contains no pages" else "INVALID_PDF"}
    except (fitz.FileDataError, fitz.EmptyFileError):
        response = {"error": "INVALID_PDF"}
    except Exception:
        response = {"error": "UNAVAILABLE"}
    print(json.dumps(response), flush=True)


def main():
    if sys.argv[1] == "--child":
        _child(sys.argv[2])
        return 0
    from app.core.native_supervisor import main as supervise
    # Preserve the guardian's owner-death detection and private process group.
    return supervise([sys.executable, "-m", _MODULE, "--child", sys.argv[1]])


if __name__ == "__main__":
    sys.exit(main())

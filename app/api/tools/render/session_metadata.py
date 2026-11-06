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

    from app.core.native_domain import admit, staged_directory, DomainBusy, DomainUnavailable
    check()
    try:
        # Reuse the existing local native slot, without accumulating waiting
        # threads behind an unrelated OCR job. API admission is bounded too.
        with admit(check, 0, lambda: _capacity(check, 0)) as domain:
            with staged_directory(domain, prefix="pdfnest-session-metadata-") as scratch:
                env = os.environ.copy()
                env.update(TMPDIR=scratch, TMP=scratch, TEMP=scratch,
                           OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4])
                result = domain.run(
                    [sys.executable, "-m", _MODULE, str(path), str(os.getpid())],
                    timeout=METADATA_TIMEOUT_SECONDS,
                    cancellation_check=check, env=env,
                    max_output_bytes=1024, cleanup_group_on_exit=True,
                    legacy_runner=run_hardened_subprocess,
                    preview_root_fd=_preview_root_fd() if not domain.required else None,
                    preview_drain_marker=str(path.parent / "writer.drained") if not domain.required else None,
                )
                domain.drain()
            if domain.required:
                count = _verified_count(result, check)
                domain.cleanup_complete()
        if domain.required:
            check()
            return count
    except DomainBusy as exc:
        raise MetadataBusy() from exc
    except DomainUnavailable as exc:
        raise MetadataUnavailable() from exc
    except OCRTimeoutError as exc:
        raise MetadataBusy() from exc
    except subprocess.TimeoutExpired as exc:
        raise MetadataTimeout() from exc
    except OSError as exc:
        raise MetadataUnavailable() from exc
    except RuntimeError as exc:
        raise MetadataFailed() from exc

    return _verified_count(result, check)


def _verified_count(result, check):
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


def _preview_root_fd():
    from .session import _SESSION_ROOT_LOCK, assert_render_process_owner
    assert_render_process_owner()
    return _SESSION_ROOT_LOCK.fileno()


def _child(path: str) -> None:
    from app.core.native_child import apply_resource_limits
    from .child_limits import apply_render_child_limits
    apply_resource_limits()
    # Metadata creation shares preview admission, so it must consume the same
    # 512 MiB / 25 CPU-second native-child budget as page rendering.
    apply_render_child_limits()
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
    from .supervisor import ROOT_LOCK_FD_ENV, CANCEL_FD_ENV, DRAIN_FD_ENV
    if ROOT_LOCK_FD_ENV in os.environ:
        from .supervisor import supervise
        return supervise([sys.executable, "-m", _MODULE, "--child", sys.argv[1]],
                         owner_pid=int(sys.argv[2]),
                         root_lock_fd=int(os.environ[ROOT_LOCK_FD_ENV]),
                         cancel_fd=int(os.environ[CANCEL_FD_ENV]),
                         drain_fd=int(os.environ[DRAIN_FD_ENV]),
                         wall_timeout=METADATA_TIMEOUT_SECONDS)
    from app.core.native_supervisor import main as supervise
    # Preserve the guardian's owner-death detection and private process group.
    return supervise([sys.executable, "-m", _MODULE, "--child", sys.argv[1]])


if __name__ == "__main__":
    sys.exit(main())

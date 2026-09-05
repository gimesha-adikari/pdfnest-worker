from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from app.api.tools.editor.utils import get_temp_dir

logger = logging.getLogger(__name__)

ALLOWED_WORKER_PREFIXES = (
    "pdfnest-",
    "tess_",
    "pdf2docx_",
)

# The shared local object store is durable application state, not a worker
# scratch directory. It intentionally lives under /tmp for local development,
# so the janitor must not treat the `pdfnest-` prefix as sufficient ownership
# evidence for this exact directory.
PERSISTENT_WORKER_DIRECTORIES = {
    "pdfnest-storage",
}


def sweep_worker_temp_files(file_ttl_seconds: int = 3600) -> int:
    now = time.time()
    eviction_count = 0

    def _sweep_directory(dir_path: str, check_prefix: bool) -> None:
        nonlocal eviction_count
        if not os.path.exists(dir_path):
            return

        try:
            entries = os.listdir(dir_path)
        except OSError:
            return

        for name in entries:
            if check_prefix and not name.startswith(ALLOWED_WORKER_PREFIXES):
                continue
            if check_prefix and name in PERSISTENT_WORKER_DIRECTORIES:
                continue

            full_path = os.path.join(dir_path, name)

            try:
                # Symlink safety check: remove link without following
                if os.path.islink(full_path):
                    os.unlink(full_path)
                    eviction_count += 1
                    continue

                mtime = os.path.getmtime(full_path)
                if (now - mtime) > file_ttl_seconds:
                    if os.path.isdir(full_path):
                        shutil.rmtree(full_path, ignore_errors=True)
                        eviction_count += 1
                    else:
                        os.remove(full_path)
                        eviction_count += 1
            except OSError:
                continue

    # Sweep the dedicated workspace before the shared system temp directory.
    dedicated = get_temp_dir()
    if dedicated != tempfile.gettempdir():
        _sweep_directory(dedicated, check_prefix=False)

    # In the shared directory, delete only worker-owned prefixes.
    _sweep_directory(tempfile.gettempdir(), check_prefix=True)

    if eviction_count > 0:
        logger.info("[WORKER JANITOR] Reclaimed workspace disk capacity. Evicted items: %d", eviction_count)

    return eviction_count


def start_worker_janitor(check_interval_seconds: int = 900, file_ttl_seconds: int = 3600) -> threading.Thread:
    def _janitor_loop() -> None:
        logger.info("[WORKER JANITOR] Background disk sweeping daemon initialized (TTL: %ds)", file_ttl_seconds)
        while True:
            try:
                time.sleep(check_interval_seconds)
                sweep_worker_temp_files(file_ttl_seconds=file_ttl_seconds)
            except Exception as exc:
                logger.warning("[WORKER JANITOR] Unexpected error during sweep: %s", exc)

    thread = threading.Thread(target=_janitor_loop, daemon=True, name="WorkerJanitorDaemon")
    thread.start()
    return thread

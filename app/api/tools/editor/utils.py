from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def temp_file_path(prefix: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path


def cleanup_paths(*paths: str | None) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def cleanup_dirs(*paths: str | None) -> None:
    for path in paths:
        try:
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)

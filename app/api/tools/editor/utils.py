import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path


def temp_file_path(prefix: str = "", suffix: str = "") -> str:
    """Generates a secure temporary file path and immediately closes the file descriptor."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path


def cleanup_paths(*paths: str | None) -> None:
    """Safely deletes individual files without throwing errors if they don't exist."""
    for path in paths:
        if path:
            with suppress(OSError):
                Path(path).unlink(missing_ok=True)


def cleanup_dirs(*paths: str | None) -> None:
    """Safely recursively deletes directories."""
    for path in paths:
        if path:
            with suppress(OSError):
                shutil.rmtree(path, ignore_errors=True)


def ensure_parent(path: str) -> None:
    """Ensures the parent directory of a given path exists."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
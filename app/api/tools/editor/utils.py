import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path


def get_temp_dir() -> str:
    """Returns the dedicated PDFNest temporary directory path (/tmp/pdfnest-temp)."""
    target = os.path.join(tempfile.gettempdir(), "pdfnest-temp")
    os.makedirs(target, mode=0o700, exist_ok=True)
    return target


def temp_file_path(prefix: str = "", suffix: str = "") -> str:
    """Generates a secure temporary file path inside pdfnest-temp and closes the file descriptor."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=get_temp_dir())
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
from __future__ import annotations

import logging
import shutil
import tempfile
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def check_disk_space(required_bytes: int, path: str | None = None) -> None:
    """
    Inspects available disk space on the filesystem hosting path (defaulting to temp directory).
    Raises HTTPException(status_code=507, detail="Insufficient Storage") if free space is below required_bytes.
    """
    target = path or tempfile.gettempdir()
    try:
        usage = shutil.disk_usage(target)
        if usage.free < required_bytes:
            req_mb = required_bytes // (1024 * 1024)
            free_mb = usage.free // (1024 * 1024)
            logger.warning("[DISK GOVERNANCE] Rejection: Required %d MB, Available %d MB on %s", req_mb, free_mb, target)
            raise HTTPException(
                status_code=507,
                detail=f"Insufficient disk space to execute requested document operation. Required: {req_mb}MB, Available: {free_mb}MB",
            )
    except OSError:
        # Fallback if disk_usage fails: do not block execution
        pass

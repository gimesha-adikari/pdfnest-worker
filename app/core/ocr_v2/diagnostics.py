"""Safe, local-only forensic helpers for Searchable PDF failures."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)

LOCAL_ENVIRONMENTS = frozenset({"development", "local"})
FORBIDDEN_DEBUG_ENVIRONMENTS = frozenset({"canary", "staging", "production"})
DEFAULT_DIAGNOSTIC_ROOT = Path("/tmp/pdfnest-ocr-v2-diagnostics")


def _app_env() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def debug_retain_failed_render_enabled() -> bool:
    """Return true only for explicit local development opt-in."""
    if _app_env() not in LOCAL_ENVIRONMENTS or _app_env() in FORBIDDEN_DEBUG_ENVIRONMENTS:
        return False
    return os.getenv("OCR_V2_DEBUG_RETAIN_FAILED_RENDER", "").strip().lower() in {"1", "true", "yes", "on"}


def safe_exception_message(exc: BaseException) -> str:
    """Keep technical diagnostics while removing paths and control text."""
    message = " ".join(str(exc).split())
    message = re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s,;]+", "<path>", message)
    message = re.sub(r"jobs/[^\s,;]+", "<storage-key>", message)
    return message[:300]


def emit_searchable_diagnostic(
    *,
    event: str,
    job_id: str | None,
    substage: str,
    fields: dict[str, Any] | None = None,
) -> None:
    """Emit one structured safe record through the existing logging system."""
    payload: dict[str, Any] = {
        "event": event,
        "job_id": job_id,
        "profile": "SEARCHABLE_PDF_V2",
        "substage": substage,
    }
    if fields:
        payload.update(fields)
    logger.info("OCR_V2_SEARCHABLE_DIAGNOSTIC %s", json.dumps(payload, sort_keys=True, default=str))


def retain_failed_render_artifacts(
    *,
    job_id: str,
    source_pdf: str | Path,
    output_pdf: str | Path,
    metadata: Iterable[dict[str, Any]] = (),
) -> Path | None:
    """Copy only local forensic PDFs when explicitly enabled; never upload them."""
    if not debug_retain_failed_render_enabled():
        return None
    root = Path(os.getenv("OCR_V2_DEBUG_DIAGNOSTIC_DIR", str(DEFAULT_DIAGNOSTIC_ROOT)))
    target = root / job_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        source_path = Path(source_pdf)
        if source_path.is_file():
            shutil.copy2(source_path, target / "source-normalized.pdf")
        output_path = Path(output_pdf)
        if output_path.is_file():
            shutil.copy2(output_path, target / "rendered-output.pdf")
        (target / "metadata.json").write_text(json.dumps(list(metadata), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        logger.warning("OCR_V2_SEARCHABLE_FAILED_RENDER_RETAINED job_id=%s path=%s", job_id, target)
        return target
    except OSError:
        logger.exception("OCR_V2_SEARCHABLE_FAILED_RENDER_RETENTION_FAILED job_id=%s", job_id)
        return None

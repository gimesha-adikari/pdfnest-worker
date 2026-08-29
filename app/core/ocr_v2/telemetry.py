"""Small structured logging helpers; no new observability dependency."""

from __future__ import annotations

import logging
import time
from typing import Any


logger = logging.getLogger("pdfnest.ocr_v2")


def emit(event: str, **fields: Any) -> None:
    logger.info("OCR_V2 %s %s", event, fields)


class StageTimer:
    def __init__(self, event: str, **fields: Any) -> None:
        self.event = event
        self.fields = fields
        self.started = 0.0

    def __enter__(self) -> "StageTimer":
        self.started = time.monotonic()
        emit(self.event + "_START", **self.fields)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        emit(self.event + ("_FAILED" if exc else "_DONE"), elapsed_seconds=time.monotonic() - self.started, **self.fields)

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Generator

logger = logging.getLogger(__name__)

# Hard Resource Invariant: Maximum concurrent Tesseract processes across the entire worker process.
# Benchmark evidence proves that exceeding 2 concurrent Tesseract executions causes CPU thrashing
# and severe performance degradation on a 6-core host.
GLOBAL_TESSERACT_CAPACITY = int(os.environ.get("GLOBAL_TESSERACT_CAPACITY", "2"))
_tesseract_semaphore = threading.BoundedSemaphore(GLOBAL_TESSERACT_CAPACITY)


class TesseractCapacityTimeoutError(Exception):
    """Raised when Tesseract capacity cannot be acquired within the configured timeout."""
    pass


@contextmanager
def acquire_tesseract_capacity(
    cancellation_check: Callable[[], None] | None = None,
    timeout: float | None = 60.0,
) -> Generator[None, None, None]:
    """
    Context manager that acquires one global Tesseract capacity token before executing
    a Tesseract subprocess, and strictly guarantees token release upon exit.

    Cooperating cancellation is checked periodically during token acquisition attempts
    to prevent cancelled requests from hanging indefinitely behind the semaphore.
    """
    start_time = time.monotonic()
    acquired = False

    try:
        while not acquired:
            if cancellation_check is not None:
                cancellation_check()

            # Attempt non-blocking / short 0.5s acquisition to allow cancellation polling
            acquired = _tesseract_semaphore.acquire(timeout=0.5)
            if acquired:
                break

            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    logger.warning(
                        "[TESSERACT CAPACITY] Capacity pool exhausted (capacity: %d). Timeout after %.1fs.",
                        GLOBAL_TESSERACT_CAPACITY,
                        elapsed,
                    )
                    raise TesseractCapacityTimeoutError(
                        f"Tesseract capacity exhausted ({GLOBAL_TESSERACT_CAPACITY} max processes). Timed out after {timeout}s."
                    )

        logger.debug("[TESSERACT CAPACITY] Token acquired.")
        yield
    finally:
        if acquired:
            _tesseract_semaphore.release()
            logger.debug("[TESSERACT CAPACITY] Token released.")

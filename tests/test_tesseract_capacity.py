import time
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.tesseract_capacity import (
    acquire_tesseract_capacity,
    GLOBAL_TESSERACT_CAPACITY,
    TesseractCapacityTimeoutError,
    _tesseract_semaphore
)


def test_tesseract_capacity_limit():
    assert GLOBAL_TESSERACT_CAPACITY == 2

    # Acquire 2 tokens
    acquired_1 = False
    acquired_2 = False

    with acquire_tesseract_capacity():
        acquired_1 = True
        with acquire_tesseract_capacity():
            acquired_2 = True
            
            # Third acquisition must timeout because capacity is 2
            with pytest.raises(TesseractCapacityTimeoutError):
                with acquire_tesseract_capacity(timeout=0.2):
                    pass

    assert acquired_1 and acquired_2


def test_tesseract_capacity_released_after_exception():
    with pytest.raises(RuntimeError):
        with acquire_tesseract_capacity():
            raise RuntimeError("Simulated failure inside Tesseract execution")

    # Verify semaphore token was released and can be acquired again
    acquired = False
    with acquire_tesseract_capacity(timeout=0.5):
        acquired = True
    assert acquired


def test_tesseract_capacity_cancellation():
    def cancel_check():
        raise RuntimeError("Cancelled mid-flight")

    with pytest.raises(RuntimeError, match="Cancelled mid-flight"):
        with acquire_tesseract_capacity(cancellation_check=cancel_check):
            pass

    # Verify token was released
    acquired = False
    with acquire_tesseract_capacity(timeout=0.5):
        acquired = True
    assert acquired


def test_max_simultaneous_tesseract_invocations():
    active_invocations = 0
    max_observed_active = 0
    lock = threading.Lock()

    def simulated_tesseract_call(idx: int):
        nonlocal active_invocations, max_observed_active
        with acquire_tesseract_capacity(timeout=5.0):
            with lock:
                active_invocations += 1
                if active_invocations > max_observed_active:
                    max_observed_active = active_invocations
            time.sleep(0.1)
            with lock:
                active_invocations -= 1

    # Spawn 8 concurrent threads trying to run Tesseract calls
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(simulated_tesseract_call, i) for i in range(8)]
        for future in as_completed(futures):
            future.result()

    # Hardware invariant check: Active invocations must NEVER exceed 2
    assert max_observed_active <= 2

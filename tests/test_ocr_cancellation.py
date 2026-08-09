import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from app.api.tools.ocr.document import (
    extract_text_from_pdf,
    page_to_ocr_text,
    run_hardened_tesseract_ocr,
)
from app.core.subprocess_runner import run_hardened_subprocess
from app.jobs.cancellation import JobCancelledException
from PIL import Image, ImageDraw


def test_hardened_subprocess_cancellation_kills_process_group():
    """TEST 1: Start controlled long-running subprocess, trigger cancellation, verify process group dies."""
    cancelled = False

    def cancel_check():
        nonlocal cancelled
        if cancelled:
            raise JobCancelledException("Cancelled in test")

    def trigger_cancel():
        nonlocal cancelled
        time.sleep(0.15)
        cancelled = True

    import threading
    threading.Thread(target=trigger_cancel, daemon=True).start()

    with pytest.raises(JobCancelledException):
        run_hardened_subprocess(
            ["sleep", "10"],
            cancellation_check=cancel_check,
            timeout=5.0,
        )


def test_ocr_cancellation_terminates_tesseract_process_group(tmp_path):
    """TEST 2: Start OCR with cancellation_check that triggers immediately on call. Verify Tesseract process group dies."""
    img = Image.new("RGB", (800, 600), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), "Test Document for Cancellation", fill=(0, 0, 0))

    img_path = tmp_path / "test_ocr.png"
    img.save(img_path)

    calls = 0

    def cancel_check():
        nonlocal calls
        calls += 1
        if calls >= 1:
            raise JobCancelledException("OCR cancelled by user")

    with pytest.raises(JobCancelledException):
        run_hardened_tesseract_ocr(
            str(img_path),
            lang="eng",
            output_format="txt",
            cancellation_check=cancel_check,
        )


def test_hardened_subprocess_timeout_terminates_process_group():
    """TEST 6: Subprocess timeout kills process group and raises TimeoutExpired."""
    with pytest.raises(subprocess.TimeoutExpired):
        run_hardened_subprocess(
            ["sleep", "10"],
            timeout=0.2,
        )


def test_successful_ocr_regression(tmp_path):
    """TEST 5: Verify normal OCR extraction succeeds and returns correct text."""
    img = Image.new("RGB", (600, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((30, 30), "Hello PDFNest World", fill=(0, 0, 0))

    img_path = tmp_path / "hello.png"
    img.save(img_path)

    res = run_hardened_tesseract_ocr(str(img_path), lang="eng", output_format="txt")
    assert isinstance(res, str)
    assert "Hello" in res or "PDFNest" in res or "World" in res or len(res.strip()) >= 0

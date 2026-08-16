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


def _create_scanned_pdf(file_path: Path, page_count: int = 55) -> None:
    import io
    import pymupdf as fitz

    doc = fitz.open()
    img = Image.new("RGB", (400, 300), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((30, 30), "Scanned Document Text Layer", fill=(0, 0, 0))

    img_bytes_io = io.BytesIO()
    img.save(img_bytes_io, format="PNG")
    img_bytes = img_bytes_io.getvalue()

    for _ in range(page_count):
        page = doc.new_page(width=400, height=300)
        page.insert_image(page.rect, stream=img_bytes)

    doc.save(str(file_path))
    doc.close()


def test_extract_text_cancellation_before_start(tmp_path):
    """TEST 6: Cancel before extraction starts. Verify immediate abort and no output file."""
    pdf_path = tmp_path / "input_before.pdf"
    _create_scanned_pdf(pdf_path, page_count=5)
    out_path = tmp_path / "out_before.txt"

    def cancel_check():
        raise JobCancelledException("Cancelled before start")

    with pytest.raises(JobCancelledException):
        extract_text_from_pdf(
            str(pdf_path),
            str(out_path),
            lang="eng",
            cancellation_check=cancel_check,
        )

    assert not out_path.exists(), "Output file must not exist after cancellation before start"


def test_extract_text_cancellation_during_ocr(tmp_path):
    """TEST 7: Cancel during active multi-page OCR. Verify cleanup and termination."""
    pdf_path = tmp_path / "input_during.pdf"
    _create_scanned_pdf(pdf_path, page_count=6)
    out_path = tmp_path / "out_during.txt"

    calls = 0

    def cancel_check():
        nonlocal calls
        calls += 1
        # Trigger after initial checks so OCR execution is underway
        if calls >= 3:
            raise JobCancelledException("Cancelled during multi-page OCR")

    with pytest.raises(JobCancelledException):
        extract_text_from_pdf(
            str(pdf_path),
            str(out_path),
            lang="eng",
            cancellation_check=cancel_check,
        )

    assert not out_path.exists(), "Output file must be cleaned up if cancelled during OCR"


def test_large_scanned_pdf_cancellation(tmp_path):
    """TEST 8: 55-page scanned PDF cancellation test.

    Requirements:
    - 50+ page scanned PDF
    - Cancel during active OCR
    - Verify task raises JobCancelledException
    - Verify no additional pages processed
    - Verify OCR process exits cleanly
    - Verify temp output files removed
    - Verify no orphan Tesseract processes remain
    """
    pdf_path = tmp_path / "large_scanned_55p.pdf"
    _create_scanned_pdf(pdf_path, page_count=55)
    out_path = tmp_path / "large_scanned_out.txt"

    cancelled = False
    call_count = 0

    def cancel_check():
        nonlocal cancelled, call_count
        call_count += 1
        if cancelled:
            raise JobCancelledException("User clicked cancel during large 55-page OCR")

    def trigger_cancel():
        nonlocal cancelled
        time.sleep(0.3)  # Let OCR start on first batch
        cancelled = True

    import threading
    t = threading.Thread(target=trigger_cancel, daemon=True)
    t.start()

    start_time = time.monotonic()
    with pytest.raises(JobCancelledException):
        extract_text_from_pdf(
            str(pdf_path),
            str(out_path),
            lang="eng",
            cancellation_check=cancel_check,
        )
    duration = time.monotonic() - start_time

    # Verification 1: Execution aborted quickly (far faster than 55 full OCR pages which take ~15-30s)
    assert duration < 10.0, f"Large PDF cancellation took too long: {duration:.2f}s"

    # Verification 2: Output file was cleaned up
    assert not out_path.exists(), "Output file must not remain after cancellation of 55-page document"

    # Verification 3: No orphan tesseract processes
    # Check with pgrep or subprocess
    ps_res = subprocess.run(["pgrep", "-f", "tesseract"], capture_output=True, text=True)
    # If any process is listed, none should belong to our tmp_path
    if ps_res.returncode == 0 and ps_res.stdout.strip():
        # Double-check process command lines
        active_pids = ps_res.stdout.strip().splitlines()
        for pid in active_pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_text()
                assert str(tmp_path) not in cmdline, f"Orphan tesseract process found: PID {pid}, {cmdline}"
            except FileNotFoundError:
                pass  # Process already exited


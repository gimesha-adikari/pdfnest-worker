import asyncio
import io
import os
import subprocess
import threading

import pytest
from fastapi import UploadFile

from app.api.tools.pdf_to_office import service
from app.api.tools.pdf_to_office.converters import word


def test_native_word_timeout_cleans_job_directory(monkeypatch, tmp_path):
    # A controlled converter simulates a hung native parser without relying on
    # a pathological PDF or changing the real OCR/SDK engine.
    (tmp_path / "pdf2docx.py").write_text(
        "import time\nclass Converter:\n"
        " def __init__(self,*a): pass\n"
        " def convert(self,*a,**k): time.sleep(60)\n"
        " def close(self): pass\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(word, "PDF_TO_WORD_TIMEOUT_SECONDS", 0.2)
    original = word.run_hardened_subprocess
    directories = []
    def capture(*args, **kwargs):
        directories.append(kwargs["cwd"])
        return original(*args, **kwargs)
    monkeypatch.setattr(word, "run_hardened_subprocess", capture)
    with pytest.raises(subprocess.TimeoutExpired):
        word._run_pdf2docx_isolated("input.pdf", "output.docx", 2)
    assert directories and all(not os.path.exists(path) for path in directories)


def test_office_conversion_does_not_block_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    def convert(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise RuntimeError("HTTP loop could not progress")
        return "converted"
    monkeypatch.setattr(service.OfficeConversionService, "_convert_sync", convert)
    async def scenario():
        file = UploadFile(filename="test.pdf", file=io.BytesIO(b"%PDF-"))
        pending = asyncio.create_task(service.OfficeConversionService.convert("docx", file))
        for _ in range(200):
            if started.is_set(): break
            await asyncio.sleep(0.005)
        assert started.is_set()
        release.set()
        assert await pending == "converted"
        await file.close()
    asyncio.run(scenario())

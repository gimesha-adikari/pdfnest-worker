"""Typed rejection through public/job boundaries with no storage/network work."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.ocr_v2.errors import RasterAdmissionError, ConfigurationError


@pytest.mark.parametrize("endpoint", ["/text", "/markup/preview"])
@pytest.mark.parametrize("error,code,status", [(RasterAdmissionError, "INVALID_INPUT", 422), (ConfigurationError, "INVALID_CONFIGURATION", 500)])
def test_public_errors_are_safe_existing_envelopes(endpoint, error, code, status, tmp_path, monkeypatch):
    from app.api.ocr_v2 import router as module
    source = tmp_path / "staged.pdf"
    source.write_bytes(b"%PDF-fake")
    async def stage(*args): return str(source)
    async def reject(*args): raise error("secret text /private/document.pdf")
    monkeypatch.setattr(module, "_stage_pdf", stage)
    monkeypatch.setattr(module, "_run_worker", reject)
    monkeypatch.setattr(module, "_run_markup_preview", reject)
    monkeypatch.setattr(module, "create_route_cancellation_checker", lambda *args: (lambda: None, lambda: None))
    monkeypatch.setattr(module.PPOCRv6MediumAdapter, "availability", lambda *args: SimpleNamespace(available=True))
    monkeypatch.setattr(module.TesseractAdapter, "availability", lambda *args: SimpleNamespace(available=True))
    app = FastAPI()
    app.include_router(module.router)
    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            return await client.post("/internal/ocr/v2" + endpoint,
                files={"file": ("input.pdf", b"%PDF", "application/pdf")},
                data={"request_id":"raster-test", "language":"eng", "profile":"MARKUP_V2" if "markup" in endpoint else "OCR_TEXT_V2"})
    response = asyncio.run(post())
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert "secret" not in response.text and "/private" not in response.text


@pytest.mark.parametrize("profile", ["OCR_TEXT_V2", "DOCUMENT_EXTRACTION_V2", "PDF_MARKDOWN_V2", "SEARCHABLE_PDF_V2", "MARKUP_V2"])
def test_job_rejection_cannot_publish_artifact_or_success(profile, tmp_path, monkeypatch):
    from app.jobs import actors
    from app.jobs.models import JobState
    updates, uploads = [], []
    monkeypatch.setattr(actors, "get_job", lambda *args: SimpleNamespace(status=JobState.queued, owner_identity="guest:test", payload={}, total_pages=1))
    monkeypatch.setattr(actors, "claim_job", lambda *args: object())
    monkeypatch.setattr(actors, "check_cancellation", lambda *args: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda *args: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda *args: None)
    monkeypatch.setattr(actors, "_cleanup_input_objects", lambda *args: None)
    paths = []
    def path(*args, **kwargs):
        value = str(tmp_path / (str(len(paths)) + kwargs.get("suffix", ".tmp")))
        paths.append(value)
        return value
    monkeypatch.setattr(actors, "temp_file_path", path)
    monkeypatch.setattr(actors, "download_to_path", lambda key, value: Path(value).write_bytes(b"%PDF-fake"))
    monkeypatch.setattr(actors, "cleanup_paths", lambda *args: None)
    monkeypatch.setattr(actors, "upload_text", lambda *args, **kw: uploads.append("json"))
    monkeypatch.setattr(actors, "upload_path", lambda *args, **kw: uploads.append("pdf"))
    monkeypatch.setattr(actors, "update_job", lambda job, **kw: updates.append(kw))
    from app.core import native_operations
    monkeypatch.setattr(native_operations, "image_source_metadata", lambda *args: ())
    reached = []
    def reject(*args, **kwargs):
        reached.append(True)
        raise RasterAdmissionError("secret text /private/document.pdf")
    for name in ("execute_ocr_text", "execute_document_extraction", "execute_pdf_to_markdown", "execute_searchable_pdf", "execute_ocr_markup"):
        monkeypatch.setattr(actors, name, reject)
    actors.ocr_v2_job("123e4567-e89b-12d3-a456-426614174000", "jobs/source.pdf", "input.pdf", "eng", "FAST", profile=profile,
                       source_files=[{"source_key":"jobs/input.png","source_name":"input.png"}], markup_action="highlight", markup_query="Text")
    assert reached == [True]
    assert uploads == []
    assert not any(update.get("status") == JobState.succeeded for update in updates)
    assert updates[-1]["status"] == JobState.failed
    assert updates[-1]["error_code"] == "INVALID_INPUT"
    assert "secret" not in updates[-1]["error"] and "/private" not in updates[-1]["error"]

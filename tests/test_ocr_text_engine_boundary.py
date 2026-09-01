from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.ocr_text_engine as engine
from app.api.ocr_v2.router import _response
from app.api.ocr_v2.schemas import OCRV2WorkerRequest
from app.core.config import validate_runtime_config
from app.jobs.models import JobState
from app.jobs.actors import ocr_v2_job


def test_engine_selector_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.OCR_TEXT_ENGINE_ENV, raising=False)

    assert engine.configured_ocr_text_engine() == "internal"


def test_engine_selector_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, " SDK ")

    assert engine.configured_ocr_text_engine() == "sdk"


def test_engine_selector_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.OCRTextEngineConfigurationError):
        engine.configured_ocr_text_engine()


def test_runtime_config_rejects_invalid_engine_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, "not-an-engine")

    with pytest.raises(engine.OCRTextEngineConfigurationError):
        validate_runtime_config()


def test_internal_selector_uses_internal_worker_and_forwards_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeWorker:
        def process_document(self, path: str, **kwargs: object) -> object:
            calls["path"] = path
            calls.update(kwargs)
            return marker

    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_worker", lambda policy: calls.setdefault("policy", policy) and FakeWorker())

    result = engine.execute_ocr_text(
        "/tmp/input.pdf",
        language="auto",
        language_mode="AUTO",
        languages=("eng", "sin"),
        language_usage={"sin": 0.75},
        routing_policy="FAST",
        cancellation_check=lambda: None,
        page_timeout_seconds=12.5,
        page_progress_callback=lambda _done, _total, _page: None,
    )

    assert result is marker
    assert calls["path"] == "/tmp/input.pdf"
    assert calls["language"] == "auto"
    assert calls["language_mode"] == "AUTO"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"sin": 0.75}
    assert calls["page_timeout_seconds"] == 12.5
    assert calls["cancellation_check"] is not None
    assert calls["page_progress_callback"] is not None
    assert calls["policy"].preferred_engine == "tesseract_v2"


def test_sdk_selector_uses_public_processor_api_and_forwards_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeProcessor:
        def extract_text(self, path: str, **kwargs: object) -> object:
            calls["path"] = path
            calls.update(kwargs)
            return marker

    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())

    result = engine.execute_ocr_text(
        "/tmp/input.pdf",
        language="eng+sin",
        language_mode="EXPLICIT",
        languages=("eng", "sin"),
        language_usage={"eng": 1.0},
        routing_policy="QUALITY",
    )

    assert result is marker
    assert calls["path"] == "/tmp/input.pdf"
    assert calls["language"] == "eng+sin"
    assert calls["language_mode"] == "EXPLICIT"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"eng": 1.0}
    assert calls["routing_policy"] == "QUALITY"
    assert calls["cancellation_check"] is None
    assert calls["page_timeout_seconds"] is None
    assert calls["page_progress_callback"] is None


def test_sdk_unavailability_is_explicit_without_internal_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_TEXT_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_processor",
        lambda: (_ for _ in ()).throw(engine.OCRTextEngineUnavailableError("SDK unavailable")),
    )

    with pytest.raises(engine.OCRTextEngineUnavailableError):
        engine.execute_ocr_text("/tmp/input.pdf", language="eng")


def test_worker_response_projection_accepts_engine_neutral_result_shape() -> None:
    page = SimpleNamespace(
        page_index=0,
        page_id="page-0",
        provenance_refs=("pymupdf_native_extractor",),
        processing_source=SimpleNamespace(value="NATIVE_EXTRACTION"),
        status=SimpleNamespace(value="SUCCESS"),
        content_classification=SimpleNamespace(value="TEXT_NATIVE"),
        text="SDK-compatible result",
        language=SimpleNamespace(
            requested_languages=("eng",),
            detected_languages=(),
            language_status="REQUESTED",
            requested_mode="EXPLICIT",
            detection_confidence=None,
            detected_scripts=(),
            detection_reason=None,
        ),
        failure_code=None,
    )
    result = SimpleNamespace(
        pages=(page,),
        validation=SimpleNamespace(valid=True),
    )
    request = OCRV2WorkerRequest(request_id="boundary", language="eng")

    response = _response(result, request, [])

    assert response.status == "SUCCEEDED"
    assert response.text == "SDK-compatible result"
    assert response.pages[0].source == "pymupdf_native_extractor"


def test_durable_ocr_text_actor_uses_the_same_engine_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: dict[str, object] = {}
    updates: list[dict[str, object]] = []
    page = SimpleNamespace(
        page_index=0,
        page_id="page-0",
        provenance_refs=("pymupdf_native_extractor",),
        processing_source=SimpleNamespace(value="NATIVE_EXTRACTION"),
        status=SimpleNamespace(value="SUCCESS"),
        content_classification=SimpleNamespace(value="TEXT_NATIVE"),
        text="durable boundary",
        language=SimpleNamespace(
            requested_languages=("eng",),
            detected_languages=(),
            language_status="REQUESTED",
            requested_mode="EXPLICIT",
            detection_confidence=None,
            detected_scripts=(),
            detection_reason=None,
        ),
        failure_code=None,
    )
    result = SimpleNamespace(
        pages=(page,),
        validation=SimpleNamespace(valid=True),
        source=SimpleNamespace(page_count=1),
    )

    def execute(path: str, **kwargs: object) -> object:
        calls["path"] = path
        calls.update(kwargs)
        return result

    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="user:alice", payload={}))
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", lambda **_kwargs: str(tmp_path / "input.pdf"))
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"%PDF-"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda _path: None)
    monkeypatch.setattr("app.jobs.actors.upload_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_ocr_text", execute)

    monkeypatch.setenv("OCR_TEXT_ENGINE", "sdk")
    ocr_v2_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "jobs/ocr_v2/input/document.pdf",
        "document.pdf",
        "eng",
        "FAST",
    )

    assert calls["path"] == str(tmp_path / "input.pdf")
    assert calls["language"] == "eng"
    assert calls["routing_policy"] == "FAST"
    assert calls["cancellation_check"] is not None
    assert calls["page_progress_callback"] is not None
    assert updates[-1]["status"] == JobState.succeeded

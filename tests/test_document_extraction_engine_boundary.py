from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.document_extraction_engine as engine
from app.jobs.models import JobState


def test_document_extraction_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, raising=False)

    assert engine.configured_document_extraction_engine() == "internal"


def test_document_extraction_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, " SDK ")

    assert engine.configured_document_extraction_engine() == "sdk"


def test_document_extraction_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.DocumentExtractionEngineConfigurationError):
        engine.configured_document_extraction_engine()


def test_internal_boundary_forwards_structured_lifecycle_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeProcessor:
        def process_document(self, path: str | Path, **kwargs: object) -> object:
            calls["path"] = path
            calls.update(kwargs)
            return marker

    cancel = lambda: None
    progress = lambda _done, _total, _page: None
    monkeypatch.setenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_processor", lambda: FakeProcessor())

    result = engine.execute_document_extraction(
        tmp_path / "source.pdf",
        language="eng+sin",
        language_mode="EXPLICIT",
        languages=("eng", "sin"),
        language_usage={"sin": 0.5},
        routing_policy="AUTO",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert result is marker
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["language"] == "eng+sin"
    assert calls["language_mode"] == "EXPLICIT"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"sin": 0.5}
    assert calls["routing_policy"] == "AUTO"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress


def test_sdk_boundary_uses_public_document_processor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeProcessor:
        def extract_document(self, path: str | Path, **kwargs: object) -> object:
            calls["path"] = path
            calls.update(kwargs)
            return marker

    cancel = lambda: None
    progress = lambda _done, _total, _page: None
    monkeypatch.setenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_internal_processor", lambda: pytest.fail("SDK mode must not construct the internal processor"))

    result = engine.execute_document_extraction(
        tmp_path / "source.pdf",
        language="auto",
        language_mode="AUTO",
        languages=("eng", "sin"),
        language_usage={"sin": 0.75},
        routing_policy="AUTO",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert result is marker
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["language"] == "auto"
    assert calls["language_mode"] == "AUTO"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"sin": 0.75}
    assert calls["routing_policy"] == "AUTO"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress


def test_sdk_failure_is_not_silently_fallen_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcessor:
        def extract_document(self, _path: str | Path, **_kwargs: object) -> object:
            raise RuntimeError("selected SDK failed")

    monkeypatch.setenv(engine.DOCUMENT_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_internal_processor", lambda: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_document_extraction(tmp_path / "source.pdf", language="eng")


def test_document_extraction_actor_delegates_only_that_profile_to_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.jobs.actors import _run_structured_document_job

    calls: dict[str, object] = {}
    updates: list[dict[str, object]] = []
    page = SimpleNamespace(page_index=0, status="SUCCESS", warnings=())
    result = SimpleNamespace(
        pages=(page,),
        warnings=(),
        validation={"valid": True},
        to_dict=lambda: {"schema_version": "ocr_v2_structured_document.v1", "pages": []},
    )

    def execute(path: str | Path, **kwargs: object) -> object:
        calls["path"] = path
        calls.update(kwargs)
        callback = kwargs["page_progress_callback"]
        assert callable(callback)
        callback(1, 1, page)
        return result

    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="guest:boundary", payload={}))
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", lambda **_kwargs: str(tmp_path / "input.pdf"))
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"pdf"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr("app.jobs.actors.upload_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_document_extraction", execute)

    _run_structured_document_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "jobs/ocr_v2/structured/input/document.pdf",
        "document.pdf",
        "eng",
        "AUTO",
        "DOCUMENT_EXTRACTION_V2",
        "AUTO",
        ["eng", "sin"],
        {"sin": 0.5},
    )

    assert calls["language"] == "eng"
    assert calls["language_mode"] == "AUTO"
    assert calls["languages"] == ["eng", "sin"]
    assert calls["language_usage"] == {"sin": 0.5}
    assert calls["routing_policy"] == "AUTO"
    assert updates[-1]["status"] == JobState.succeeded


def test_pdf_markdown_profile_keeps_internal_structured_processor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.jobs.actors import _run_structured_document_job
    from app.core.ocr_v2 import structured as structured_module

    calls: list[str] = []
    updates: list[dict[str, object]] = []
    page = SimpleNamespace(page_index=0, status="SUCCESS", warnings=())
    result = SimpleNamespace(
        pages=(page,),
        warnings=(),
        validation={"valid": True},
        to_dict=lambda: {"schema_version": "ocr_v2_structured_document.v1", "pages": []},
    )

    class FakeProcessor:
        def process_document(self, _path: str | Path, **_kwargs: object) -> object:
            calls.append("internal")
            return result

    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="guest:boundary", payload={}))
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", lambda **_kwargs: str(tmp_path / "input.pdf"))
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"pdf"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr("app.jobs.actors.upload_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_document_extraction", lambda *_args, **_kwargs: pytest.fail("PDF Markdown must not use Document Extraction selector"))
    monkeypatch.setattr(structured_module, "StructuredDocumentProcessor", FakeProcessor)
    monkeypatch.setattr(structured_module, "render_structured_markdown", lambda _result: "# internal")

    _run_structured_document_job(
        "123e4567-e89b-12d3-a456-426614174001",
        "jobs/ocr_v2/structured/input/document.pdf",
        "document.pdf",
        "eng",
        "AUTO",
        "PDF_MARKDOWN_V2",
    )

    assert calls == ["internal"]
    assert updates[-1]["status"] == JobState.succeeded

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.ocr_markup_engine as engine
from app.core.ocr_v2.errors import TextNotFoundError as WorkerTextNotFoundError
from app.jobs.models import JobState


def test_markup_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.OCR_MARKUP_ENGINE_ENV, raising=False)

    assert engine.configured_ocr_markup_engine() == "internal"


def test_markup_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, " SDK ")

    assert engine.configured_ocr_markup_engine() == "sdk"


def test_markup_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.OcrMarkupEngineConfigurationError, match="OCR_MARKUP_ENGINE"):
        engine.configured_ocr_markup_engine()


def test_internal_markup_boundary_forwards_the_family_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    def execute_internal(input_path: str | Path, output_path: str | Path, **kwargs: object) -> object:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls.update(kwargs)
        return marker

    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_execute_internal", execute_internal)

    result = engine.execute_ocr_markup(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        action="underline",
        query="Alpha Bravo",
        language="eng+sin",
        language_mode="EXPLICIT",
        languages=("eng", "sin"),
        language_usage={"sin": 0.5},
        mode="ocr",
        color=(0.1, 0.2, 0.3),
        cancellation_check=lambda: None,
        progress_callback=lambda _done, _total: None,
    )

    assert result is marker
    assert calls["input_path"] == tmp_path / "input.pdf"
    assert calls["output_path"] == tmp_path / "output.pdf"
    assert calls["action"] == "underline"
    assert calls["query"] == "Alpha Bravo"
    assert calls["language"] == "eng+sin"
    assert calls["language_mode"] == "EXPLICIT"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"sin": 0.5}
    assert calls["mode"] == "ocr"
    assert calls["color"] == (0.1, 0.2, 0.3)
    assert calls["cancellation_check"] is not None
    assert calls["progress_callback"] is not None


def test_sdk_markup_boundary_uses_public_processor_without_internal_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeProcessor:
        def apply_markup(self, input_path: str | Path, output_path: str | Path, **kwargs: object) -> object:
            calls["input_path"] = input_path
            calls["output_path"] = output_path
            calls.update(kwargs)
            return marker

    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_execute_internal", lambda *_args, **_kwargs: pytest.fail("SDK mode must not use internal markup"))

    result = engine.execute_ocr_markup(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        action="strikeout",
        query="Alpha",
        language="auto",
        language_mode="AUTO",
        languages=("eng", "sin"),
        mode="smart",
    )

    assert result is marker
    assert calls["action"] == "strikeout"
    assert calls["mode"] == "smart"
    assert calls["language"] == "auto"
    assert calls["language_mode"] == "AUTO"
    assert calls["languages"] == ("eng", "sin")


def test_sdk_failure_is_not_silently_fallen_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeProcessor:
        def apply_markup(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("selected SDK failed")

    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_execute_internal", lambda *_args, **_kwargs: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_ocr_markup(tmp_path / "input.pdf", tmp_path / "output.pdf", action="highlight", query="Alpha")


def test_sdk_typed_markup_error_maps_to_existing_worker_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from platen_document import TextNotFoundError as SdkTextNotFoundError

    class FakeProcessor:
        def apply_markup(self, *_args: object, **_kwargs: object) -> object:
            raise SdkTextNotFoundError("SDK did not find the requested text")

    monkeypatch.setenv(engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())

    with pytest.raises(WorkerTextNotFoundError, match="requested text"):
        engine.execute_ocr_markup(tmp_path / "input.pdf", tmp_path / "output.pdf", action="highlight", query="Missing")


def test_markup_actor_delegates_standalone_v2_work_to_family_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.jobs.actors import _run_markup_v2_job

    calls: dict[str, object] = {}
    updates: list[dict[str, object]] = []

    class FakeExecution:
        page_count = 1

        def to_dict(self) -> dict[str, object]:
            return {"schema_version": "ocr_v2_markup_result.v1", "selection_count": 1}

    def temporary_path(*_args: object, **kwargs: object) -> str:
        return str(tmp_path / f"temporary{kwargs.get('suffix', '.tmp')}")

    def execute(input_path: str | Path, output_path: str | Path, **kwargs: object) -> FakeExecution:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls.update(kwargs)
        Path(output_path).write_bytes(b"%PDF- boundary artifact")
        return FakeExecution()

    monkeypatch.setattr(
        "app.jobs.actors.get_job",
        lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="guest:boundary", payload={}, total_pages=1),
    )
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", temporary_path)
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"%PDF- input"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr("app.jobs.actors.upload_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.upload_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_ocr_markup", execute)

    _run_markup_v2_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "jobs/ocr_v2/markup/input/document.pdf",
        "document.pdf",
        "eng",
        "highlight",
        "smart",
        "Alpha",
        "#FFFF00",
        "EXPLICIT",
        ["eng"],
        {},
    )

    assert calls["action"] == "highlight"
    assert calls["mode"] == "smart"
    assert calls["language"] == "eng"
    assert calls["languages"] == ["eng"]
    assert calls["cancellation_check"] is not None
    assert calls["progress_callback"] is not None
    assert updates[-1]["status"] == JobState.succeeded

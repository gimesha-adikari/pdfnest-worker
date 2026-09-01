from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.searchable_pdf_engine as engine
from app.core.ocr_v2.errors import EngineUnavailableError, RenderingNotEligibleError as WorkerRenderingNotEligibleError
from app.jobs.models import JobState


def test_searchable_pdf_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.SEARCHABLE_PDF_ENGINE_ENV, raising=False)

    assert engine.configured_searchable_pdf_engine() == "internal"


def test_searchable_pdf_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, " SDK ")

    assert engine.configured_searchable_pdf_engine() == "sdk"


def test_searchable_pdf_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.SearchablePdfEngineConfigurationError):
        engine.configured_searchable_pdf_engine()


def test_internal_boundary_preserves_worker_callback_and_render_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    marker = object()

    class FakeWorker:
        def process_document(self, path: str | Path, **kwargs: object) -> object:
            calls.append(("process", (path, kwargs)))
            return marker

    class FakeRenderer:
        def render(self, source: str | Path, result: object, output: str | Path, **kwargs: object) -> None:
            calls.append(("render", (source, result, output, kwargs)))

    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_worker", lambda: FakeWorker())
    monkeypatch.setattr(engine, "_internal_renderer", lambda: FakeRenderer())

    result = engine.execute_searchable_pdf(
        tmp_path / "source.pdf",
        tmp_path / "output.pdf",
        language="eng+sin",
        language_mode="EXPLICIT",
        languages=("eng", "sin"),
        language_usage={"sin": 0.5},
        cancellation_check=lambda: None,
        page_progress_callback=lambda _done, _total, _page: None,
        after_ocr=lambda value: calls.append(("after_ocr", value)),
        diagnostic_job_id="boundary-job",
    )

    assert result is marker
    assert [name for name, _value in calls] == ["process", "after_ocr", "render"]
    process_kwargs = calls[0][1][1]  # type: ignore[index]
    assert process_kwargs["language"] == "eng+sin"
    assert process_kwargs["language_mode"] == "EXPLICIT"
    assert process_kwargs["languages"] == ("eng", "sin")
    assert process_kwargs["language_usage"] == {"sin": 0.5}
    assert process_kwargs["profile"].value == "SEARCHABLE_PDF_V2"
    render_kwargs = calls[2][1][3]  # type: ignore[index]
    assert render_kwargs["job_id"] == "boundary-job"


def test_sdk_boundary_uses_public_processor_once_and_renders_same_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    marker = object()
    profile = SimpleNamespace(value="SEARCHABLE_PDF_V2")

    class FakeProcessor:
        def extract_text(self, path: str | Path, **kwargs: object) -> object:
            calls.append(("extract_text", (path, kwargs)))
            return marker

        def make_searchable_pdf(self, source: str | Path, output: str | Path, **kwargs: object) -> None:
            calls.append(("make_searchable_pdf", (source, output, kwargs)))

    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: profile)
    monkeypatch.setattr(engine, "_internal_worker", lambda: pytest.fail("SDK mode must not construct the internal OCR worker"))
    monkeypatch.setattr(engine, "_internal_renderer", lambda: pytest.fail("SDK mode must not construct the internal PDF renderer"))

    result = engine.execute_searchable_pdf(
        tmp_path / "source.pdf",
        tmp_path / "output.pdf",
        language="auto",
        language_mode="AUTO",
        languages=("eng", "sin"),
        language_usage={"sin": 0.75},
        cancellation_check=lambda: None,
        page_progress_callback=lambda _done, _total, _page: None,
        diagnostic_job_id="sdk-boundary-job",
    )

    assert result is marker
    assert [name for name, _value in calls] == ["extract_text", "make_searchable_pdf"]
    extract_kwargs = calls[0][1][1]  # type: ignore[index]
    assert extract_kwargs["profile"] is profile
    assert extract_kwargs["routing_policy"] == "FAST"
    assert extract_kwargs["language"] == "auto"
    assert extract_kwargs["language_mode"] == "AUTO"
    assert extract_kwargs["languages"] == ("eng", "sin")
    render_kwargs = calls[1][1][2]  # type: ignore[index]
    assert render_kwargs == {"result": marker, "job_id": "sdk-boundary-job"}


def test_sdk_rendering_error_maps_to_worker_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = object()

    class RenderingNotEligibleError(Exception):
        def __init__(self) -> None:
            super().__init__("SDK render rejected the result")
            self.substage = "PDF_RENDER_PROFILE_CHECK"
            self.reason_code = "PROFILE_NOT_ELIGIBLE"

    class FakeProcessor:
        def extract_text(self, _path: str | Path, **_kwargs: object) -> object:
            return marker

        def make_searchable_pdf(self, _source: str | Path, _output: str | Path, **_kwargs: object) -> None:
            raise RenderingNotEligibleError()

    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: SimpleNamespace(value="SEARCHABLE_PDF_V2"))

    with pytest.raises(WorkerRenderingNotEligibleError) as raised:
        engine.execute_searchable_pdf(tmp_path / "source.pdf", tmp_path / "output.pdf", language="eng")

    assert raised.value.substage == "PDF_RENDER_PROFILE_CHECK"
    assert raised.value.reason_code == "PROFILE_NOT_ELIGIBLE"


def test_sdk_engine_failure_is_not_silently_fallen_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeProcessor:
        def extract_text(self, _path: str | Path, **_kwargs: object) -> object:
            raise RuntimeError("selected SDK failed")

    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: SimpleNamespace(value="SEARCHABLE_PDF_V2"))
    monkeypatch.setattr(engine, "_internal_worker", lambda: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_searchable_pdf(tmp_path / "source.pdf", tmp_path / "output.pdf", language="eng")


def test_sdk_engine_unavailability_uses_worker_engine_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(engine.SEARCHABLE_PDF_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_processor",
        lambda: (_ for _ in ()).throw(engine.SearchablePdfEngineUnavailableError("SDK unavailable")),
    )

    with pytest.raises(EngineUnavailableError, match="SDK unavailable"):
        engine.execute_searchable_pdf(tmp_path / "source.pdf", tmp_path / "output.pdf", language="eng")


def test_searchable_actor_delegates_ocr_and_render_to_consumer_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.jobs.actors import _run_searchable_pdf_job

    calls: dict[str, object] = {}
    updates: list[dict[str, object]] = []
    page = SimpleNamespace(
        page_index=0,
        status=SimpleNamespace(value="SUCCESS"),
        tokens=(),
        reading_order=(),
        geometry=SimpleNamespace(width=100.0, height=100.0),
    )
    result = SimpleNamespace(
        pages=(page,),
        source=SimpleNamespace(page_count=1),
        validation=SimpleNamespace(valid=True, issues=()),
    )

    def temporary_path(*_args: object, **kwargs: object) -> str:
        suffix = str(kwargs.get("suffix", ".tmp"))
        path = tmp_path / f"temporary-{len(calls)}{suffix}"
        calls[f"path_{len(calls)}"] = str(path)
        return str(path)

    def execute(source: str | Path, output: str | Path, **kwargs: object) -> object:
        calls["source"] = str(source)
        calls["output"] = str(output)
        calls.update(kwargs)
        callback = kwargs["after_ocr"]
        assert callable(callback)
        callback(result)
        Path(output).write_bytes(b"%PDF- boundary artifact")
        return result

    monkeypatch.setattr(
        "app.jobs.actors.get_job",
        lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="guest:boundary", payload={}),
    )
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", temporary_path)
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"image"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr("app.jobs.actors.upload_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_searchable_pdf", execute)
    monkeypatch.setattr(
        "app.core.ocr_v2.image_pages.build_image_source_pdf",
        lambda _inputs, _output: (SimpleNamespace(format="PNG", width=10, height=10, page_width=4.8, page_height=4.8),),
    )

    _run_searchable_pdf_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "eng",
        [{"source_key": "jobs/ocr_v2/searchable_pdf/input/page.png", "source_name": "page.png"}],
        "page.png",
    )

    assert calls["language"] == "eng"
    assert calls["language_mode"] == "EXPLICIT"
    assert calls["languages"] == []
    assert calls["diagnostic_job_id"] == "123e4567-e89b-12d3-a456-426614174000"
    assert updates[-1]["status"] == JobState.succeeded

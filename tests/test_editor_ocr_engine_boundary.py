from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.editor_ocr_engine as engine
import app.jobs.actors as actors
from app.api.tools.editor.router import ExtractRequest
from app.jobs.models import JobState


def test_editor_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.EDITOR_OCR_ENGINE_ENV, raising=False)

    assert engine.configured_editor_ocr_engine() == "internal"


def test_editor_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.EDITOR_OCR_ENGINE_ENV, " SDK ")

    assert engine.configured_editor_ocr_engine() == "sdk"


def test_editor_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.EDITOR_OCR_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.EditorOcrEngineConfigurationError):
        engine.configured_editor_ocr_engine()


def test_editor_extract_request_rejects_unknown_consumer() -> None:
    with pytest.raises(ValueError):
        ExtractRequest(source_key="jobs/editor/source.pdf", consumer="other")


def test_internal_editor_boundary_forwards_password_and_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    def execute(path: str, password: str | None, **kwargs: object) -> object:
        calls["path"] = path
        calls["password"] = password
        calls.update(kwargs)
        return marker

    monkeypatch.setenv(engine.EDITOR_OCR_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_execute", execute)
    cancel = lambda: None
    progress = lambda _done, _total, _page: None

    result = engine.execute_editor_ocr(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert result is marker
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["password"] == "document-password"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress


def test_sdk_editor_boundary_uses_public_processor_and_projects_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    sdk_result = SimpleNamespace(pages=())
    projected = {"success": True, "schema_version": "ocr_v2_editor_layout.v1", "pages": []}

    class FakeProcessor:
        def extract_text(self, path: str | Path, **kwargs: object) -> object:
            calls["extract_text_calls"] = int(calls.get("extract_text_calls", 0)) + 1
            calls["path"] = path
            calls.update(kwargs)
            return sdk_result

    monkeypatch.setenv(engine.EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: "OCR_TEXT_V2")
    monkeypatch.setattr(engine, "project_editor_result", lambda result: projected)
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: pytest.fail("SDK mode must not call the internal editor path"),
    )

    cancel = lambda: None
    progress = lambda _done, _total, _page: None
    result = engine.execute_editor_ocr(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert result is projected
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["password"] == "document-password"
    assert calls["language"] == "eng"
    assert calls["profile"] == "OCR_TEXT_V2"
    assert calls["routing_policy"] == "FAST"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress
    assert calls["extract_text_calls"] == 1


def test_sdk_editor_failure_is_not_silently_fallen_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(engine.EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_execute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("selected SDK failed")),
    )
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: pytest.fail("SDK failure must not fall back to internal"),
    )

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_editor_ocr(tmp_path / "source.pdf")


def test_editor_projection_is_engine_neutral() -> None:
    word = SimpleNamespace(
        id="w1",
        text="Alpha",
        bbox=SimpleNamespace(x=10.0, y=20.0, width=35.0, height=12.0),
        confidence=SimpleNamespace(raw_value=0.9),
    )
    line = SimpleNamespace(
        text="Alpha",
        bbox=SimpleNamespace(x=10.0, y=20.0, width=35.0, height=12.0),
        token_ids=("w1",),
    )
    page = SimpleNamespace(
        page_index=0,
        geometry=SimpleNamespace(width=612.0, height=792.0),
        content_classification="IMAGE_SCAN",
        processing_source="OCR_RECOGNITION",
        tokens=(word,),
        tokens_by_id={"w1": word},
        lines=(line,),
        reading_order=("w1",),
        provenance_refs=("tesseract_v2",),
        capabilities=frozenset({"TEXT", "WORD_GEOMETRY"}),
    )
    result = SimpleNamespace(
        pages=(page,),
        source=SimpleNamespace(to_dict=lambda: {"page_count": 1, "filename": "source.pdf"}),
    )

    projected = engine.project_editor_result(result)

    assert projected["schema_version"] == "ocr_v2_editor_layout.v1"
    assert projected["pages"][0]["kind"] == "scanned"
    assert projected["pages"][0]["is_ocr"] is True
    assert projected["pages"][0]["elements"][0]["word_geometry"][0]["id"] == "w1"


def _patch_editor_actor_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, updates: list[dict[str, object]]) -> None:
    monkeypatch.setattr(
        actors,
        "get_job",
        lambda _job_id: SimpleNamespace(
            status=JobState.queued,
            owner_identity="user:editor",
            payload={},
        ),
    )
    monkeypatch.setattr(actors, "check_cancellation", lambda _job_id: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr(actors, "temp_file_path", lambda **_kwargs: str(tmp_path / "input.pdf"))
    monkeypatch.setattr(actors, "download_to_path", lambda _key, path: Path(path).write_bytes(b"pdf"))
    monkeypatch.setattr(actors, "cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr(actors, "update_job", lambda _job_id, **fields: updates.append(fields))


def test_editor_actor_routes_general_editor_only_to_selected_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[str] = []

    def sdk_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("sdk")
        return {"success": True}

    monkeypatch.setattr(actors, "execute_editor_ocr", sdk_execute)
    monkeypatch.setattr(
        actors,
        "extract_document_v2",
        lambda *_args, **_kwargs: pytest.fail("General Editor must not use the direct internal helper"),
    )

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "jobs/editor/source.pdf",
        None,
        "source.pdf",
        True,
        "general_editor",
    )

    assert calls == ["sdk"]
    assert updates[-1]["status"] == JobState.succeeded


def test_editor_actor_routes_studio_to_its_own_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[str] = []

    monkeypatch.setattr(
        actors,
        "execute_editor_ocr",
        lambda *_args, **_kwargs: pytest.fail(
            "Studio must not use the General Editor SDK boundary"
        ),
    )

    def studio_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("studio")
        return {"success": True}

    monkeypatch.setattr(actors, "execute_studio_editor_extraction", studio_execute)

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174001",
        "jobs/studio/source.pdf",
        None,
        "source.pdf",
        True,
        "studio",
    )

    assert calls == ["studio"]
    assert updates[-1]["status"] == JobState.succeeded

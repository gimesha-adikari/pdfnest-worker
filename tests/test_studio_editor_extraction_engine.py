from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.studio_editor_extraction_engine as engine
import app.jobs.actors as actors
from app.jobs.models import JobState


def test_studio_editor_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, raising=False)

    assert engine.configured_studio_editor_extraction_engine() == "internal"


def test_studio_editor_engine_blank_value_defaults_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "   ")

    assert engine.configured_studio_editor_extraction_engine() == "internal"


def test_studio_editor_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, " SDK ")

    assert engine.configured_studio_editor_extraction_engine() == "sdk"


def test_studio_editor_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "external")

    with pytest.raises(engine.StudioEditorExtractionEngineConfigurationError):
        engine.configured_studio_editor_extraction_engine()


def test_internal_studio_editor_path_forwards_password_and_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    def execute(path: str | Path, password: str | None, **kwargs: object) -> object:
        calls["path"] = path
        calls["password"] = password
        calls.update(kwargs)
        return marker

    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_execute", execute)
    cancel = lambda: None
    progress = lambda _done, _total, _page: None

    assert engine.execute_studio_editor_extraction(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=cancel,
        page_progress_callback=progress,
    ) is marker
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["password"] == "document-password"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress


def test_sdk_studio_editor_path_uses_public_processor_projection_and_callbacks(
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
            kwargs["cancellation_check"]()
            kwargs["page_progress_callback"](1, 1, object())
            return sdk_result

    progress_calls: list[tuple[int, int]] = []
    cancel_calls: list[bool] = []
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: "OCR_TEXT_V2")
    monkeypatch.setattr(engine, "project_editor_result", lambda result: projected)
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: pytest.fail("SDK mode must not invoke the internal Studio path"),
    )

    result = engine.execute_studio_editor_extraction(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=lambda: cancel_calls.append(True),
        page_progress_callback=lambda done, total, _page: progress_calls.append((done, total)),
    )

    assert result is projected
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["password"] == "document-password"
    assert calls["language"] == "eng"
    assert calls["profile"] == "OCR_TEXT_V2"
    assert calls["routing_policy"] == "FAST"
    assert calls["extract_text_calls"] == 1
    assert cancel_calls == [True]
    assert progress_calls == [(1, 1)]


def test_sdk_studio_editor_failure_has_no_internal_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
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
        engine.execute_studio_editor_extraction(tmp_path / "source.pdf")


def _patch_editor_actor_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    updates: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(
        actors,
        "get_job",
        lambda _job_id: SimpleNamespace(
            status=JobState.queued,
            owner_identity="user:studio",
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


def test_editor_actor_routes_only_studio_to_studio_selector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[str] = []

    monkeypatch.setattr(actors, "execute_studio_editor_extraction", lambda *_args, **_kwargs: calls.append("studio") or {"success": True})
    monkeypatch.setattr(actors, "execute_editor_ocr", lambda *_args, **_kwargs: pytest.fail("Studio must not use General Editor selector"))
    monkeypatch.setattr(actors, "extract_document_v2", lambda *_args, **_kwargs: pytest.fail("Studio must not bypass its selector"))

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174021",
        "jobs/studio/source.pdf",
        None,
        "source.pdf",
        True,
        "studio",
    )

    assert calls == ["studio"]
    assert updates[-1]["status"] == JobState.succeeded


def test_studio_selector_does_not_change_general_editor_actor_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[str] = []
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "sdk")
    monkeypatch.setattr(actors, "execute_editor_ocr", lambda *_args, **_kwargs: calls.append("general") or {"success": True})
    monkeypatch.setattr(actors, "execute_studio_editor_extraction", lambda *_args, **_kwargs: pytest.fail("General Editor must not use Studio selector"))

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174022",
        "jobs/editor/source.pdf",
        None,
        "source.pdf",
        True,
        "general_editor",
    )

    assert calls == ["general"]
    assert updates[-1]["status"] == JobState.succeeded


def test_editor_selector_does_not_change_studio_engine_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("EDITOR_OCR_ENGINE", "sdk")
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "internal")
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: calls.append("studio-internal") or {"success": True},
    )
    monkeypatch.setattr(
        engine,
        "_sdk_execute",
        lambda *_args, **_kwargs: pytest.fail("Studio must not inherit EDITOR_OCR_ENGINE"),
    )

    engine.execute_studio_editor_extraction(tmp_path / "source.pdf")

    assert calls == ["studio-internal"]


def test_invalid_studio_selector_is_controlled_actor_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    monkeypatch.setenv(engine.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV, "unsupported")

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174023",
        "jobs/studio/source.pdf",
        None,
        "source.pdf",
        True,
        "studio",
    )

    assert updates[-1]["status"] == JobState.failed
    assert updates[-1]["error_code"] == "INVALID_CONFIGURATION"

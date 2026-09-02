from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pymupdf as fitz

import app.core.legacy_editor_ocr_engine as engine
import app.jobs.actors as actors
from app.api.tools.editor.router import ExtractRequest
from app.jobs.models import JobState


def test_legacy_editor_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, raising=False)

    assert engine.configured_legacy_editor_ocr_engine() == "internal"


def test_legacy_editor_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, " SDK ")

    assert engine.configured_legacy_editor_ocr_engine() == "sdk"


def test_legacy_editor_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "external")

    with pytest.raises(engine.LegacyEditorOcrEngineConfigurationError):
        engine.configured_legacy_editor_ocr_engine()


def test_editor_extract_request_accepts_legacy_editor_marker() -> None:
    request = ExtractRequest(
        source_key="jobs/editor/source.pdf",
        consumer=engine.LEGACY_EDITOR_CONSUMER,
    )

    assert request.consumer == engine.LEGACY_EDITOR_CONSUMER
    assert request.ocr_v2 is False


def test_legacy_internal_boundary_preserves_password_and_cancellation(
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

    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_execute", execute)
    cancellation = lambda: None

    result = engine.execute_legacy_editor_ocr(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=cancellation,
        page_progress_callback=None,
    )

    assert result is marker
    assert calls == {
        "path": tmp_path / "source.pdf",
        "password": "document-password",
        "cancellation_check": cancellation,
        "page_progress_callback": None,
    }


def test_legacy_sdk_boundary_uses_public_processor_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    sdk_result = SimpleNamespace(pages=())
    projected = {"success": True, "pages": []}

    class FakeProcessor:
        def extract_text(self, path: str | Path, **kwargs: object) -> object:
            calls["extract_text_calls"] = int(calls.get("extract_text_calls", 0)) + 1
            calls["path"] = path
            calls.update(kwargs)
            return sdk_result

    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_sdk_profile", lambda: "OCR_TEXT_V2")
    monkeypatch.setattr(engine, "project_editor_result", lambda result: projected)
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy PDF Editor SDK mode must not call the internal path"
        ),
    )
    cancellation = lambda: None

    result = engine.execute_legacy_editor_ocr(
        tmp_path / "source.pdf",
        "document-password",
        cancellation_check=cancellation,
        page_progress_callback=None,
    )

    assert result is projected
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["password"] == "document-password"
    assert calls["language"] == "eng"
    assert calls["profile"] == "OCR_TEXT_V2"
    assert calls["routing_policy"] == "FAST"
    assert calls["cancellation_check"] is cancellation
    assert calls["extract_text_calls"] == 1


def test_legacy_sdk_failure_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_execute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("selected legacy SDK failed")
        ),
    )
    monkeypatch.setattr(
        engine,
        "_internal_execute",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy SDK failure must not fall back to internal"
        ),
    )

    with pytest.raises(RuntimeError, match="selected legacy SDK failed"):
        engine.execute_legacy_editor_ocr(tmp_path / "source.pdf")


def test_legacy_sdk_native_only_path_preserves_native_first_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "native.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((30, 80), "Native control", fontsize=20)
    document.save(source)
    document.close()

    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_processor",
        lambda: pytest.fail("native-only legacy input must not invoke OCR SDK processing"),
    )

    result = engine.execute_legacy_editor_ocr(source)

    assert result["success"] is True
    assert result["pages"][0]["kind"] == "text"
    assert result["pages"][0]["has_selectable_text"] is True


def test_legacy_sdk_native_preflight_honors_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "native.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((30, 80), "Cancellation control", fontsize=20)
    document.save(source)
    document.close()

    from app.jobs.cancellation import JobCancelledException

    def cancel() -> None:
        raise JobCancelledException("cancelled")

    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(
        engine,
        "_sdk_processor",
        lambda: pytest.fail("cancellation must stop before SDK processing"),
    )

    with pytest.raises(JobCancelledException, match="cancelled"):
        engine.execute_legacy_editor_ocr(source, cancellation_check=cancel)


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
            owner_identity="user:editor",
            payload={},
        ),
    )
    monkeypatch.setattr(actors, "check_cancellation", lambda _job_id: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr(
        actors,
        "temp_file_path",
        lambda **_kwargs: str(tmp_path / "input.pdf"),
    )
    monkeypatch.setattr(
        actors,
        "download_to_path",
        lambda _key, path: Path(path).write_bytes(b"pdf"),
    )
    monkeypatch.setattr(actors, "cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr(actors, "update_job", lambda _job_id, **fields: updates.append(fields))


def test_legacy_editor_actor_uses_legacy_selector_and_not_shared_editor_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[str] = []

    def selected_legacy(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append("legacy")
        assert "cancellation_check" in kwargs
        assert "page_progress_callback" not in kwargs
        return {"success": True}

    monkeypatch.setattr(actors, "execute_legacy_editor_ocr", selected_legacy)
    monkeypatch.setattr(
        actors,
        "execute_editor_ocr",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy Editor must not use the General Editor boundary"
        ),
    )
    monkeypatch.setattr(
        actors,
        "extract_document_v2",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy Editor must not use the direct V2 helper"
        ),
    )

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174010",
        "jobs/editor/source.pdf",
        None,
        "source.pdf",
        False,
        engine.LEGACY_EDITOR_CONSUMER,
    )

    assert calls == ["legacy"]
    assert updates[-1]["status"] == JobState.succeeded


def test_private_legacy_marker_keeps_historical_extractor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    calls: list[dict[str, object]] = []

    def historical_extract(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(actors, "extract_document", historical_extract)
    monkeypatch.setattr(
        actors,
        "execute_legacy_editor_ocr",
        lambda *_args, **_kwargs: pytest.fail(
            "generic legacy compatibility callers must not use the new selector"
        ),
    )

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174011",
        "jobs/editor/source.pdf",
        None,
        "source.pdf",
        False,
        "legacy",
    )

    assert len(calls) == 1
    assert "cancellation_check" in calls[0]
    assert "page_progress_callback" not in calls[0]
    assert updates[-1]["status"] == JobState.succeeded


def test_legacy_selector_does_not_change_general_editor_or_studio_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    updates: list[dict[str, object]] = []
    _patch_editor_actor_io(monkeypatch, tmp_path, updates)
    monkeypatch.setenv(engine.LEGACY_EDITOR_OCR_ENGINE_ENV, "sdk")
    calls: list[str] = []

    monkeypatch.setattr(
        actors,
        "execute_legacy_editor_ocr",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy selector must not affect other consumers"
        ),
    )
    monkeypatch.setattr(
        actors,
        "execute_editor_ocr",
        lambda *_args, **_kwargs: (calls.append("general") or {"success": True}),
    )
    monkeypatch.setattr(
        actors,
        "extract_document_v2",
        lambda *_args, **_kwargs: (calls.append("studio") or {"success": True}),
    )

    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174012",
        "jobs/editor/source.pdf",
        None,
        "source.pdf",
        True,
        "general_editor",
    )
    actors.editor_extract_job(
        "123e4567-e89b-12d3-a456-426614174013",
        "jobs/studio/source.pdf",
        None,
        "source.pdf",
        True,
        "studio",
    )

    assert calls == ["general", "studio"]

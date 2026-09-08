from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.pdf_to_markdown_engine as engine
from app.jobs.models import JobState


def test_pdf_to_markdown_engine_defaults_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, raising=False)

    assert engine.configured_pdf_to_markdown_engine() == "internal"


def test_pdf_to_markdown_engine_accepts_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, " SDK ")

    assert engine.configured_pdf_to_markdown_engine() == "sdk"


def test_pdf_to_markdown_engine_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "future-engine")

    with pytest.raises(engine.PdfToMarkdownEngineConfigurationError):
        engine.configured_pdf_to_markdown_engine()


def test_pdf_to_markdown_processors_enable_scanned_table_capability() -> None:
    internal = engine._internal_processor()
    sdk = engine._sdk_processor()

    assert internal.enable_scanned_table_recognition is True
    assert sdk.config.enable_scanned_table_recognition is True


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_pdf_to_markdown_reuses_page_scoped_raster_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selected: str,
) -> None:
    calls: dict[str, object] = {}
    marker = object()

    class FakeProcessor:
        def process_document(self, _path: str | Path, **_kwargs: object) -> object:
            return marker

        def extract_document(self, _path: str | Path, **_kwargs: object) -> object:
            return marker

        def to_markdown(self, _result: object, *, emit_page_breaks: bool) -> str:
            assert emit_page_breaks is True
            return "# markdown"

    def processor_factory(**kwargs: object) -> FakeProcessor:
        calls.update(kwargs)
        return FakeProcessor()

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)
    monkeypatch.setattr(engine, "_pdf_to_markdown_raster_dpis", lambda _path: (200, 72, 200))
    if selected == "internal":
        monkeypatch.setattr(engine, "_internal_processor", processor_factory)
        monkeypatch.setattr(engine, "_internal_markdown", lambda _result: "# markdown")
    else:
        monkeypatch.setattr(engine, "_sdk_processor", processor_factory)

    execution = engine.execute_pdf_to_markdown(tmp_path / "source.pdf", language="eng")

    assert execution.structured_result is marker
    assert calls == {"raster_dpis": (200, 72, 200)}


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_pdf_to_markdown_projects_original_scanned_table(
    monkeypatch: pytest.MonkeyPatch,
    selected: str,
) -> None:
    source = Path("/home/gimesha/My_Projects/platen/benchmarks/ocr_eval/fixtures_generated/synth_table_scan.pdf")
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)

    execution = engine.execute_pdf_to_markdown(source, language="eng", routing_policy="FAST")

    tables = [
        element
        for page in execution.structured_result.pages
        for element in page.elements
        if getattr(element.type, "value", element.type) == "TABLE"
    ]
    assert len(tables) == 1
    table = tables[0]
    assert table.data["column_count"] == 6
    assert table.data["row_count"] == 4
    assert [cell["text"] for cell in table.data["headers"]] == ["ID", "Name", "Q1", "Q2", "Q3", "Total"]
    assert [[cell["text"] for cell in row] for row in table.data["rows"]] == [
        ["001", "Alice", "10", "15", "12", "37"],
        ["002", "Bob", "8", "9", "11", "28"],
        ["003", "Charlie", "20", "18", "22", "60"],
        ["004", "Dave", "5", "5", "5", "15"],
    ]
    assert execution.markdown.count("| ID | Name | Q1 | Q2 | Q3 | Total |") == 1
    assert "\n\n| 001 |" not in execution.markdown


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_pdf_to_markdown_does_not_turn_mixed_cv_content_into_a_table(
    monkeypatch: pytest.MonkeyPatch,
    selected: str,
) -> None:
    source = Path("/home/gimesha/My_Projects/platen/pdfnest/tests/fixtures/gimesha_cv.pdf")
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)

    execution = engine.execute_pdf_to_markdown(source, language="eng", routing_policy="FAST")

    assert all(
        getattr(element.type, "value", element.type) != "TABLE"
        for page in execution.structured_result.pages
        for element in page.elements
    )
    assert "BankingSystem" in execution.markdown
    assert "\n| " not in execution.markdown


def test_internal_boundary_forwards_structured_controls_and_renders_once(
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

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_processor", lambda: FakeProcessor())

    def render(result: object) -> str:
        calls["rendered_result"] = result
        return "# internal"

    monkeypatch.setattr(engine, "_internal_markdown", render)

    cancel = lambda: None
    progress = lambda _done, _total, _page: None
    execution = engine.execute_pdf_to_markdown(
        tmp_path / "source.pdf",
        language="eng+sin",
        language_mode="EXPLICIT",
        languages=("eng", "sin"),
        language_usage={"sin": 0.5},
        routing_policy="AUTO",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert execution.structured_result is marker
    assert execution.markdown == "# internal"
    assert calls["path"] == tmp_path / "source.pdf"
    assert calls["language"] == "eng+sin"
    assert calls["language_mode"] == "EXPLICIT"
    assert calls["languages"] == ("eng", "sin")
    assert calls["language_usage"] == {"sin": 0.5}
    assert calls["routing_policy"] == "AUTO"
    assert calls["cancellation_check"] is cancel
    assert calls["page_progress_callback"] is progress
    assert calls["rendered_result"] is marker


def test_sdk_boundary_uses_public_processor_and_one_structured_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    marker = object()

    class FakeProcessor:
        def extract_document(self, path: str | Path, **kwargs: object) -> object:
            calls.append(("extract_document", (path, kwargs)))
            return marker

        def to_markdown(self, result: object, *, emit_page_breaks: bool) -> str:
            calls.append(("to_markdown", (result, emit_page_breaks)))
            assert result is marker
            return "# sdk"

        def extract_markdown(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("PDF-to-Markdown SDK mode must not run a second extraction")

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_internal_processor", lambda: pytest.fail("SDK mode must not construct the internal processor"))

    cancel = lambda: None
    progress = lambda _done, _total, _page: None
    execution = engine.execute_pdf_to_markdown(
        tmp_path / "source.pdf",
        language="auto",
        language_mode="AUTO",
        languages=("eng", "sin"),
        language_usage={"sin": 0.75},
        routing_policy="AUTO",
        cancellation_check=cancel,
        page_progress_callback=progress,
    )

    assert execution.structured_result is marker
    assert execution.markdown == "# sdk"
    assert [name for name, _value in calls] == ["extract_document", "to_markdown"]
    extract_args = calls[0][1]
    assert isinstance(extract_args, tuple)
    assert extract_args[0] == tmp_path / "source.pdf"
    assert extract_args[1]["language"] == "auto"
    assert extract_args[1]["language_mode"] == "AUTO"
    assert extract_args[1]["languages"] == ("eng", "sin")
    assert extract_args[1]["language_usage"] == {"sin": 0.75}
    assert extract_args[1]["routing_policy"] == "AUTO"
    assert extract_args[1]["cancellation_check"] is cancel
    assert extract_args[1]["page_progress_callback"] is progress


def test_sdk_failure_is_not_silently_fallen_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcessor:
        def extract_document(self, _path: str | Path, **_kwargs: object) -> object:
            raise RuntimeError("selected SDK failed")

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(engine, "_internal_processor", lambda: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_pdf_to_markdown(tmp_path / "source.pdf", language="eng")


def test_actor_uses_pdf_to_markdown_boundary_without_document_selector(
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

    def execute(path: str | Path, **kwargs: object) -> engine.PdfToMarkdownExecution:
        calls["path"] = path
        calls.update(kwargs)
        callback = kwargs["page_progress_callback"]
        assert callable(callback)
        callback(1, 1, page)
        return engine.PdfToMarkdownExecution(result, "# sdk markdown")

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
    monkeypatch.setattr("app.jobs.actors.execute_document_extraction", lambda *_args, **_kwargs: pytest.fail("PDF-to-Markdown must not use Document Extraction selector"))
    monkeypatch.setattr("app.jobs.actors.execute_pdf_to_markdown", execute)

    _run_structured_document_job(
        "123e4567-e89b-12d3-a456-426614174002",
        "jobs/ocr_v2/structured/input/document.pdf",
        "document.pdf",
        "eng",
        "AUTO",
        "PDF_MARKDOWN_V2",
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

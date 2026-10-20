from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest

import app.core.pdf_to_markdown_engine as engine
from app.jobs.models import JobState


def _scanned_table_pdf(path: Path) -> None:
    """A ruled six-column table, embedded as pixels without a native text layer."""
    rows = [
        ["ID", "Name", "Q1", "Q2", "Q3", "Total"],
        ["001", "Alice", "10", "15", "12", "37"],
        ["002", "Bob", "8", "9", "11", "28"],
        ["003", "Charlie", "20", "18", "22", "60"],
        ["004", "Dave", "5", "5", "5", "15"],
    ]
    with fitz.open() as native:
        # Retain the original table's cell/glyph spacing; crop only blank
        # space below it so OCR still exercises the same exact cell values.
        page = native.new_page(width=595, height=300)
        for row_index in range(5):
            for column in range(6):
                x, y = 50 + column * 80, 70 + row_index * 40
                page.draw_rect(fitz.Rect(x, y, x + 80, y + 40), color=(0, 0, 0), width=2)
        for row_index, row in enumerate(rows):
            for column, text in enumerate(row):
                x, y = 50 + column * 80, 70 + row_index * 40
                page.insert_text((x + 5, y + 25), text, fontsize=12, fontname="helv")
        raster = page.get_pixmap(dpi=300, alpha=False)
        width, height = raster.width, raster.height
        assert width * height == 3_100_000
        encoded = raster.tobytes("png")
        del raster
    with fitz.open() as document:
        # The original scan uses one PDF point per source pixel. Preserve
        # that adaptive-DPI path without its 8.7-million-pixel full-page image.
        page = document.new_page(width=width, height=height)
        page.insert_image(page.rect, stream=encoded)
        assert not page.get_text().strip()
        document.save(path)


def _non_tabular_cv_pdf(path: Path) -> None:
    """CV-style aligned dates, varied prose blocks and headings, without a grid."""
    with fitz.open() as document:
        page = document.new_page(width=600, height=450)
        page.insert_text((40, 40), "Alex Example", fontsize=20, fontname="hebo")
        page.insert_text((40, 68), "Software engineer and document processing specialist", fontsize=11)
        page.insert_text((40, 105), "PROJECT EXPERIENCE", fontsize=14, fontname="hebo")
        page.insert_text((40, 139), "2022 - 2024", fontsize=10)
        page.insert_text((155, 139), "BankingSystem", fontsize=13, fontname="hebo")
        remaining = page.insert_textbox(
            fitz.Rect(155, 153, 555, 210),
            "Built account services and reviewed transaction workflows. "
            "Coordinated three services with authentication and clear error handling. "
            "Documented recovery procedures for the engineering team.",
            fontsize=11,
        )
        assert remaining >= 0
        page.insert_text((40, 230), "2020 - 2022", fontsize=10)
        page.insert_text((155, 230), "Document workflow platform", fontsize=13, fontname="hebo")
        remaining = page.insert_textbox(
            fitz.Rect(155, 245, 555, 310),
            "Implemented searchable reports and accessible editing tools. "
            "Worked with support teams to resolve document conversion problems "
            "and improve the reliability of background processing.",
            fontsize=11,
        )
        assert remaining >= 0
        page.insert_text((40, 340), "SKILLS", fontsize=14, fontname="hebo")
        remaining = page.insert_textbox(
            fitz.Rect(40, 360, 555, 420),
            "Python and SQL for service development. Practical experience with "
            "document geometry, regression testing and operational documentation.",
            fontsize=11,
        )
        assert remaining >= 0
        document.save(path)


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
    tmp_path: Path,
) -> None:
    source = tmp_path / "scanned-table.pdf"
    _scanned_table_pdf(source)
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)

    execution = engine.execute_pdf_to_markdown(source, language="eng", routing_policy="FAST")

    assert execution.structured_result.pages[0].processing_source == "OCR_RECOGNITION"
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
    tmp_path: Path,
) -> None:
    source = tmp_path / "project-summary.pdf"
    _non_tabular_cv_pdf(source)
    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)

    execution = engine.execute_pdf_to_markdown(source, language="eng", routing_policy="FAST")

    assert execution.structured_result.pages[0].processing_source == "NATIVE_EXTRACTION"
    assert "Document workflow platform" in execution.markdown
    assert "regression testing" in execution.markdown
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


def test_short_markdown_projection_is_repaired_from_canonical_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sentinel projection must not replace meaningful structured content."""

    element = SimpleNamespace(
        element_id="paragraph-1",
        type=SimpleNamespace(value="PARAGRAPH"),
        page_index=0,
        text="The controlled fixture has meaningful text.",
        level=None,
        ordered=None,
        data={},
    )
    page = SimpleNamespace(
        page_index=0,
        geometry={"width": 600.0, "height": 800.0},
        elements=(element,),
        reading_order=("paragraph-1",),
    )
    result = SimpleNamespace(pages=(page,), elements=(element,))
    sentinel = engine.PdfToMarkdownExecution(result, "a")

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_execute_internal", lambda *_args, **_kwargs: sentinel)

    execution = engine.execute_pdf_to_markdown(tmp_path / "source.pdf", language="eng")

    assert execution.markdown != "a"
    assert "controlled fixture" in execution.markdown
    assert execution.markdown.endswith("\n")


def test_projection_repair_preserves_unicode_and_newlines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    element = SimpleNamespace(
        element_id="paragraph-1",
        type=SimpleNamespace(value="TEXT_BLOCK"),
        page_index=0,
        text="Résumé Δ\nLine two",
        level=None,
        ordered=None,
        data={},
    )
    page = SimpleNamespace(
        page_index=0,
        geometry={"width": 600.0, "height": 800.0},
        elements=(element,),
        reading_order=("paragraph-1",),
    )
    result = SimpleNamespace(pages=(page,), elements=(element,))
    sentinel = engine.PdfToMarkdownExecution(result, "a\n")

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_execute_sdk", lambda *_args, **_kwargs: sentinel)

    execution = engine.execute_pdf_to_markdown(tmp_path / "source.pdf", language="eng")

    assert "Résumé Δ" in execution.markdown
    assert "Line two" in execution.markdown


def test_short_canonical_document_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    element = SimpleNamespace(
        element_id="paragraph-1",
        type=SimpleNamespace(value="PARAGRAPH"),
        page_index=0,
        text="a",
        level=None,
        ordered=None,
        data={},
    )
    page = SimpleNamespace(
        page_index=0,
        geometry={"width": 600.0, "height": 800.0},
        elements=(element,),
        reading_order=("paragraph-1",),
    )
    result = SimpleNamespace(pages=(page,), elements=(element,))
    sentinel = engine.PdfToMarkdownExecution(result, "a")

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_execute_internal", lambda *_args, **_kwargs: sentinel)

    execution = engine.execute_pdf_to_markdown(tmp_path / "source.pdf", language="eng")

    assert execution.markdown == "a"


@pytest.mark.parametrize("selected", ["internal", "sdk"])
def test_legitimate_one_character_native_document_remains_valid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selected: str,
) -> None:
    """The short-output guard must not rewrite a genuine one-character PDF."""

    source = tmp_path / "one-character.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((80, 100), "a", fontsize=32, fontname="helv")
        document.save(str(source))

    monkeypatch.setenv(engine.PDF_TO_MARKDOWN_ENGINE_ENV, selected)
    execution = engine.execute_pdf_to_markdown(source, language="eng", routing_policy="FAST")

    assert execution.structured_result.validation["valid"] is True
    assert execution.markdown.strip() == "a"


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


def test_actor_serialization_round_trip_preserves_markdown_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The persisted actor payload keeps meaningful UTF-8 Markdown intact."""

    from app.jobs.actors import _run_structured_document_job

    uploads: list[tuple[str, str, dict[str, object]]] = []
    updates: list[dict[str, object]] = []
    page = SimpleNamespace(page_index=0, status="SUCCESS", warnings=())
    payload = {
        "schema_version": "ocr_v2_structured_document.v1",
        "pages": [{"page_index": 0, "elements": [{"text": "Résumé Δ\nLine two"}]}],
    }
    result = SimpleNamespace(
        pages=(page,),
        warnings=(),
        validation={"valid": True},
        to_dict=lambda: payload,
    )

    def execute(_path: str | Path, **_kwargs: object) -> engine.PdfToMarkdownExecution:
        return engine.PdfToMarkdownExecution(result, "# Résumé Δ\n\nLine two\n")

    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="guest:boundary", payload={}))
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: object())
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr("app.jobs.actors.release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr("app.jobs.actors._cleanup_input_objects", lambda _keys: None)
    monkeypatch.setattr("app.jobs.actors.temp_file_path", lambda **_kwargs: str(tmp_path / "input.pdf"))
    monkeypatch.setattr("app.jobs.actors.download_to_path", lambda _key, path: Path(path).write_bytes(b"pdf"))
    monkeypatch.setattr("app.jobs.actors.cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr("app.jobs.actors.upload_text", lambda text, key, **kwargs: uploads.append((text, key, kwargs)))
    monkeypatch.setattr("app.jobs.actors.update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr("app.jobs.actors.execute_pdf_to_markdown", execute)

    _run_structured_document_job(
        "123e4567-e89b-12d3-a456-426614174003",
        "jobs/ocr_v2/structured/input/document.pdf",
        "document.pdf",
        "eng",
        "AUTO",
        "PDF_MARKDOWN_V2",
        "AUTO",
        ["eng"],
        {},
    )

    assert updates[-1]["status"] == JobState.succeeded
    assert len(uploads) == 1
    serialized, _key, kwargs = uploads[0]
    assert kwargs["content_type"] == "application/json"
    round_trip = json.loads(serialized)
    assert round_trip["markdown"] == "# Résumé Δ\n\nLine two\n"
    assert "Résumé Δ" in json.dumps(round_trip, ensure_ascii=False)

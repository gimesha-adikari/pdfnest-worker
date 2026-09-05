from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.studio_markup_region_ocr_engine as engine
from app.jobs.cancellation import JobCancelledException
from app.core.ocr_v2.errors import TextNotFoundError


def _execution(*, mode: str = "smart") -> SimpleNamespace:
    selection = SimpleNamespace(
        to_dict=lambda: {
            "page_index": 0,
            "matched_text": "Alpha Bravo",
            "word_ids": ["word-0", "word-1"],
            "group_rects": [{"x": 10, "y": 20, "width": 90, "height": 12}],
            "source_type": "ocr",
        }
    )
    region = SimpleNamespace(
        region_index=0,
        region_id="studio-region-1",
        page_number=1,
        status=SimpleNamespace(value="annotated"),
        color=(0.1, 0.2, 0.3),
        annotation_count=1,
        word_ids=("word-0", "word-1"),
        selected_text="Alpha Bravo",
        annotation_rects=(SimpleNamespace(x=10.0, y=20.0, width=90.0, height=12.0),),
        selection=selection if mode != "manual" else None,
    )
    return SimpleNamespace(
        source_policy="MANUAL_RECTANGLES_NO_TEXT_EXTRACTION" if mode == "manual" else "EXTRACT_TEXT_ONCE_THEN_CANONICAL_REGION_SELECTION",
        regions=(region,),
        annotation_count=1,
        page_count=1,
        document_result_reused=False,
        extraction_performed=mode != "manual",
    )


def _boxes() -> list[dict[str, object]]:
    return [{"id": "studio-region-1", "page": 1, "x": 10, "y": 20, "width": 90, "height": 12, "color": "#1A334C"}]


def test_selector_defaults_blank_and_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, raising=False)
    assert engine.configured_studio_markup_region_ocr_engine() == "internal"
    assert engine.configured_studio_markup_region_ocr_engine("  ") == "internal"

    with pytest.raises(engine.StudioMarkupRegionOcrEngineConfigurationError, match="STUDIO_MARKUP_REGION_OCR_ENGINE"):
        engine.configured_studio_markup_region_ocr_engine("external")


def test_internal_selector_calls_the_unchanged_studio_region_processor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def internal(*args: object, **kwargs: object) -> dict[str, object]:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"source_policy": "OCR_V2_CANONICAL_WORDS", "selection_count": 1}

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_internal_execute", internal)
    result = engine.execute_studio_markup_region_ocr(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=_boxes(),
        action="highlight",
        mode="ocr",
        password="pass-through",
    )

    assert result["source_policy"] == "OCR_V2_CANONICAL_WORDS"
    assert calls["args"][3:6] == ("highlight", "ocr", "pass-through")  # type: ignore[index]


@pytest.mark.parametrize(
    ("action", "mode", "expected_action", "expected_mode"),
    [
        ("highlight", "manual", "highlight", "manual"),
        ("underline", "smart", "underline", "smart"),
        ("strikeout", "ocr", "strikeout", "ocr"),
    ],
)
def test_sdk_adapter_maps_actions_modes_regions_colors_and_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    mode: str,
    expected_action: str,
    expected_mode: str,
) -> None:
    calls: dict[str, object] = {}

    class Processor:
        def apply_markup_regions(self, input_path: Path, output_path: Path, **kwargs: object) -> SimpleNamespace:
            calls["input_path"] = input_path
            calls["output_path"] = output_path
            calls.update(kwargs)
            return _execution(mode=mode)

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: Processor())
    result = engine.execute_studio_markup_region_ocr(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=_boxes(),
        action=action,
        mode=mode,
        password="pass-through",
    )

    region = calls["regions"][0]  # type: ignore[index]
    assert calls["action"].value == expected_action  # type: ignore[index]
    assert calls["mode"].value == expected_mode  # type: ignore[index]
    assert region.page_number == 1
    assert region.region_id == "studio-region-1"
    assert (region.rect.x, region.rect.y, region.rect.width, region.rect.height) == (10.0, 20.0, 90.0, 12.0)
    assert region.color == pytest.approx((26 / 255, 51 / 255, 76 / 255))
    assert calls["password"] == "pass-through"
    assert calls["language"] == "eng"
    assert calls["routing_policy"] == "FAST"
    assert result["regions"][0]["region_id"] == "studio-region-1"


def test_sdk_manual_mode_uses_public_region_operation_without_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    class Processor:
        def extract_text(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("the Studio adapter must not pre-extract manual regions")

        def apply_markup_regions(self, *_args: object, **kwargs: object) -> SimpleNamespace:
            calls.append("apply_markup_regions")
            assert kwargs["mode"].value == "manual"
            assert kwargs["page_progress_callback"] is None
            assert callable(kwargs["progress_callback"])
            kwargs["progress_callback"](1, 1)
            return _execution(mode="manual")

    progress: list[tuple[int, int]] = []
    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: Processor())
    result = engine.execute_studio_markup_region_ocr(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=_boxes(),
        action="highlight",
        mode="manual",
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert calls == ["apply_markup_regions"]
    assert progress == [(1, 1)]
    assert result["extraction_performed"] is False


def test_sdk_progress_and_cancellation_are_forwarded_without_manual_ocr_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    progress: list[tuple[int, int]] = []

    class Processor:
        def apply_markup_regions(self, *_args: object, **kwargs: object) -> SimpleNamespace:
            assert callable(kwargs["cancellation_check"])
            kwargs["cancellation_check"]()
            assert callable(kwargs["page_progress_callback"])
            kwargs["page_progress_callback"](1, 2, object())
            return _execution()

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: Processor())
    engine.execute_studio_markup_region_ocr(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=_boxes(),
        action="underline",
        mode="smart",
        cancellation_check=lambda: None,
        progress_callback=lambda done, total: progress.append((done, total)),
    )
    assert progress == [(1, 2)]

    class CancelledProcessor:
        def apply_markup_regions(self, *_args: object, **kwargs: object) -> SimpleNamespace:
            kwargs["cancellation_check"]()
            raise AssertionError("cancelled SDK work must stop")

    monkeypatch.setattr(engine, "_sdk_processor", lambda: CancelledProcessor())
    with pytest.raises(JobCancelledException, match="cancelled"):
        engine.execute_studio_markup_region_ocr(
            tmp_path / "input.pdf",
            tmp_path / "output.pdf",
            boxes=_boxes(),
            action="underline",
            mode="ocr",
            cancellation_check=lambda: (_ for _ in ()).throw(JobCancelledException("cancelled")),
        )


def test_sdk_failure_does_not_fall_back_to_internal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class BrokenProcessor:
        def apply_markup_regions(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("selected SDK failed")

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: BrokenProcessor())
    monkeypatch.setattr(engine, "_internal_execute", lambda *_args, **_kwargs: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="selected SDK failed"):
        engine.execute_studio_markup_region_ocr(
            tmp_path / "input.pdf",
            tmp_path / "output.pdf",
            boxes=_boxes(),
            action="strikeout",
        )


def test_sdk_adapter_preserves_native_empty_selection_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty_region = SimpleNamespace(
        region_index=0,
        region_id="studio-region-1",
        page_number=1,
        status=SimpleNamespace(value="no_words"),
        color=(1.0, 1.0, 0.0),
        annotation_count=0,
        word_ids=(),
        selected_text="",
        annotation_rects=(),
        selection=None,
    )

    class Processor:
        def apply_markup_regions(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                source_policy="EXTRACT_TEXT_ONCE_THEN_CANONICAL_REGION_SELECTION",
                regions=(empty_region,),
                annotation_count=0,
                page_count=1,
                page_sources=({"source_type": "native"},),
                document_result_reused=False,
                extraction_performed=True,
            )

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: Processor())
    with pytest.raises(TextNotFoundError, match="no canonical words"):
        engine.execute_studio_markup_region_ocr(
            tmp_path / "input.pdf",
            tmp_path / "output.pdf",
            boxes=_boxes(),
            action="highlight",
            mode="ocr",
        )


def test_selector_is_independent_from_editor_and_other_markup_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.editor_ocr_engine import configured_editor_ocr_engine
    from app.core.legacy_markup_ocr_engine import configured_legacy_markup_ocr_engine
    from app.core.ocr_markup_engine import configured_ocr_markup_engine
    from app.core.studio_editor_extraction_engine import configured_studio_editor_extraction_engine

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "internal")
    monkeypatch.setenv("EDITOR_OCR_ENGINE", "internal")
    monkeypatch.setenv("OCR_MARKUP_ENGINE", "internal")
    monkeypatch.setenv("LEGACY_MARKUP_OCR_ENGINE", "internal")
    assert engine.configured_studio_markup_region_ocr_engine() == "sdk"
    assert configured_studio_editor_extraction_engine() == "internal"
    assert configured_editor_ocr_engine() == "internal"
    assert configured_ocr_markup_engine() == "internal"
    assert configured_legacy_markup_ocr_engine() == "internal"

    monkeypatch.setenv(engine.STUDIO_MARKUP_REGION_OCR_ENGINE_ENV, "internal")
    monkeypatch.setenv("STUDIO_EDITOR_EXTRACTION_ENGINE", "sdk")
    assert engine.configured_studio_markup_region_ocr_engine() == "internal"
    assert configured_studio_editor_extraction_engine() == "sdk"


def test_sdk_processor_uses_the_studio_raster_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import platen_document

    captured: dict[str, object] = {}

    class Processor:
        def __init__(self, config: object) -> None:
            captured["config"] = config

    monkeypatch.setattr(platen_document, "DocumentProcessor", Processor)
    engine._sdk_processor()
    assert captured["config"].max_raster_pixels == 25_000_000  # type: ignore[index]


def test_markup_actor_routes_only_studio_ocr_v2_payloads_to_the_new_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pymupdf as fitz
    import app.jobs.actors as actors
    from app.jobs.models import JobState

    source = tmp_path / "source.pdf"
    document = fitz.open()
    document.new_page(width=200, height=100).insert_text((20, 40), "Alpha")
    document.save(source)
    document.close()
    updates: list[dict[str, object]] = []
    calls: dict[str, object] = {}

    monkeypatch.setattr(actors, "get_job", lambda _job_id: SimpleNamespace(payload={"ownerIdentity": "guest:studio"}))
    monkeypatch.setattr(actors, "check_cancellation", lambda _job_id: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda _job_id, _owner: None)
    monkeypatch.setattr(actors, "build_key", lambda _prefix, suffix: f"artifact{suffix}")
    monkeypatch.setattr(actors, "upload_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(actors, "cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr(actors, "update_job", lambda _job_id, **fields: updates.append(fields))

    def download(key: str, destination: str | Path) -> None:
        destination_path = Path(destination)
        if key == "source-key":
            shutil.copyfile(source, destination_path)
        else:
            destination_path.write_text(json.dumps({"ocr_v2": True, "boxes": _boxes(), "mode": "smart"}), encoding="utf-8")

    def studio_boundary(**kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        shutil.copyfile(kwargs["input_path"], kwargs["output_path"])  # type: ignore[arg-type]
        return {"selection_count": 1}

    monkeypatch.setattr(actors, "download_to_path", download)
    monkeypatch.setattr(actors, "execute_studio_markup_region_ocr", studio_boundary)
    monkeypatch.setattr(actors, "process_markup_pdf", lambda *_args, **_kwargs: pytest.fail("Studio payload must not use historical markup"))

    actors._run_markup_job("123e4567-e89b-12d3-a456-426614174001", "source-key", "payload-key", "source.pdf", "highlight")

    assert calls["action"] == "highlight"
    assert calls["mode"] == "smart"
    assert calls["boxes"] == _boxes()
    assert updates[-1]["status"] is JobState.succeeded

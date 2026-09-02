from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.legacy_markup_ocr_engine as engine


def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        pages=(
            SimpleNamespace(
                page_index=0,
                status=SimpleNamespace(value="SUCCESS"),
                geometry=SimpleNamespace(width=300.0, height=300.0, pixel_width=600, pixel_height=600),
                tokens=(
                    SimpleNamespace(
                        text="Scanned",
                        bbox=SimpleNamespace(x=10.0, y=20.0, width=42.0, height=12.0),
                        confidence=SimpleNamespace(raw_value=91.0),
                    ),
                ),
            ),
        )
    )


def test_legacy_markup_selector_defaults_and_rejects_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, raising=False)
    assert engine.configured_legacy_markup_ocr_engine() == "internal"

    with pytest.raises(engine.LegacyMarkupOcrEngineConfigurationError, match="LEGACY_MARKUP_OCR_ENGINE"):
        engine.configured_legacy_markup_ocr_engine("external")


def test_sdk_legacy_markup_extracts_once_and_reuses_public_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    class FakeProcessor:
        def extract_text(self, path: str | Path, **kwargs: object) -> SimpleNamespace:
            calls["path"] = path
            calls.update(kwargs)
            return _fake_result()

    def project_markup(**kwargs: object) -> None:
        calls["markup"] = kwargs

    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: FakeProcessor())
    monkeypatch.setattr(
        "app.api.tools.markup.document.process_markup_pdf_with_ocr_words",
        project_markup,
    )

    result = engine.execute_legacy_markup(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=[{"page": 1, "x": 1, "y": 2, "width": 80, "height": 40}],
        action="highlight",
        mode="ocr",
    )

    assert result == {"engine": "platen_document", "ocr_passes": 1, "page_count": 1, "word_pages": 1}
    assert calls["path"] == tmp_path / "input.pdf"
    assert calls["password"] is None
    assert calls["language"] == "eng"
    assert calls["routing_policy"] == "FORCE_OCR"
    assert calls["markup"]["action"] == "highlight"  # type: ignore[index]
    words = calls["markup"]["ocr_word_items_by_page"][0]  # type: ignore[index]
    assert len(words) == 1
    assert words[0]["text"] == "Scanned"
    assert words[0]["rect"] == engine.fitz.Rect(10, 20, 52, 32)


def test_sdk_failure_does_not_fall_back_to_historical_markup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: (_ for _ in ()).throw(RuntimeError("selected SDK failed")))
    monkeypatch.setattr(engine, "_historical_markup", lambda *_args, **_kwargs: pytest.fail("SDK failure must not fall back"))

    with pytest.raises(RuntimeError, match="legacy markup SDK processing failed"):
        engine.execute_legacy_markup(
            tmp_path / "input.pdf",
            tmp_path / "output.pdf",
            boxes=[],
            action="underline",
            mode="smart",
        )


def test_sdk_legacy_markup_preserves_cooperative_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.jobs.cancellation import JobCancelledException

    class CancelledProcessor:
        def extract_text(self, _path: str | Path, **kwargs: object) -> SimpleNamespace:
            check = kwargs["cancellation_check"]
            assert callable(check)
            check()
            raise AssertionError("cancelled extraction must not continue")

    def cancel() -> None:
        raise JobCancelledException("cancelled")

    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setattr(engine, "_sdk_processor", lambda: CancelledProcessor())
    monkeypatch.setattr(
        "app.api.tools.markup.document.process_markup_pdf_with_ocr_words",
        lambda **_kwargs: pytest.fail("cancelled SDK work must not reach markup projection"),
    )

    with pytest.raises(JobCancelledException, match="cancelled"):
        engine.execute_legacy_markup(
            tmp_path / "input.pdf",
            tmp_path / "output.pdf",
            boxes=[],
            action="highlight",
            mode="ocr",
            cancellation_check=cancel,
        )


def test_internal_legacy_markup_keeps_historical_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def historical(*args: object, **kwargs: object) -> None:
        calls["args"] = args
        calls["kwargs"] = kwargs

    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "internal")
    monkeypatch.setattr(engine, "_historical_markup", historical)

    result = engine.execute_legacy_markup(
        tmp_path / "input.pdf",
        tmp_path / "output.pdf",
        boxes=[],
        action="strikeout",
        mode="manual",
    )

    assert result == {"engine": "internal", "ocr_passes": 0}
    assert calls["kwargs"]["mode"] == "manual"  # type: ignore[index]


def test_markup_actor_keeps_legacy_marker_separate_from_studio() -> None:
    from app.jobs.actors import _markup_processing_route

    assert _markup_processing_route({"consumer": engine.LEGACY_MARKUP_CONSUMER, "ocr_v2": True}) == engine.LEGACY_MARKUP_CONSUMER
    assert _markup_processing_route({"ocr_v2": True}) == "studio_ocr_v2"
    assert _markup_processing_route({}) == "historical"


def test_legacy_markup_selector_is_independent_from_v2_markup_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.ocr_markup_engine as v2_engine

    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "sdk")
    monkeypatch.setenv(v2_engine.OCR_MARKUP_ENGINE_ENV, "internal")
    assert engine.configured_legacy_markup_ocr_engine() == "sdk"
    assert v2_engine.configured_ocr_markup_engine() == "internal"

    monkeypatch.setenv(engine.LEGACY_MARKUP_OCR_ENGINE_ENV, "internal")
    monkeypatch.setenv(v2_engine.OCR_MARKUP_ENGINE_ENV, "sdk")
    assert engine.configured_legacy_markup_ocr_engine() == "internal"
    assert v2_engine.configured_ocr_markup_engine() == "sdk"

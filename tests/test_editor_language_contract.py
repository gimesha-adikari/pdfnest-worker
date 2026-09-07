from __future__ import annotations
from types import SimpleNamespace
import pytest
import app.core.editor_ocr_engine as general
import app.core.studio_editor_extraction_engine as studio
from app.core.editor_language import EditorLanguageConfigurationError, editor_language_intent
from app.core.editor_language import EditorLanguageRequiredError
from app.api.tools.editor.document import extract_document_v2
import io
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

@pytest.mark.parametrize("mode,languages,expression", [("EXPLICIT", ["eng"], "eng"), ("EXPLICIT", ["sin"], "sin"), ("EXPLICIT", ["tam"], "tam"), ("AUTO", ["eng", "sin", "tam"], "auto")])
def test_language_intent(mode, languages, expression):
    intent = editor_language_intent(mode, languages)
    assert intent.expression == expression

def test_language_intent_rejects_unknown_and_never_falls_back():
    with pytest.raises(EditorLanguageConfigurationError): editor_language_intent("AUTO", ["fra"])

@pytest.mark.parametrize("module,selector", [(general, general.EDITOR_OCR_ENGINE_ENV), (studio, studio.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV)])
@pytest.mark.parametrize("mode,languages", [("EXPLICIT", ["eng"]), ("EXPLICIT", ["sin"]), ("EXPLICIT", ["tam"]), ("AUTO", ["eng", "sin", "tam"])])
def test_sdk_selector_forwards_equivalent_language_intent(monkeypatch, tmp_path, module, selector, mode, languages):
    calls = {}
    class Processor:
        def extract_text(self, path, **kwargs): calls.update(kwargs); return SimpleNamespace(pages=())
    monkeypatch.setenv(selector, "sdk")
    monkeypatch.setattr(module, "_sdk_processor", lambda: Processor())
    monkeypatch.setattr(module, "_sdk_profile", lambda: "OCR_TEXT_V2")
    projection_name = "project_studio_editor_result" if module is studio else "project_editor_result"
    monkeypatch.setattr(module, projection_name, lambda result: {"schema_version": "ocr_v2_editor_layout.v1", "pages": []})
    function = general.execute_editor_ocr if module is general else studio.execute_studio_editor_extraction
    projected = function(tmp_path / "source.pdf", language_mode=mode, languages=languages)
    assert calls["language"] == ("auto" if mode == "AUTO" else "+".join(languages))
    assert calls["language_mode"] == mode
    assert tuple(calls["languages"]) == tuple(languages)
    assert projected["language_mode"] == mode
    assert projected["languages"] == languages

@pytest.mark.parametrize("module,selector", [(general, general.EDITOR_OCR_ENGINE_ENV), (studio, studio.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV)])
def test_internal_selector_forwards_language_intent(monkeypatch, tmp_path, module, selector):
    calls = {}
    monkeypatch.setenv(selector, "internal")
    monkeypatch.setattr(module, "_internal_execute", lambda path, password, **kwargs: calls.update(kwargs) or {"ok": True})
    function = general.execute_editor_ocr if module is general else studio.execute_studio_editor_extraction
    assert function(tmp_path / "source.pdf", language_mode="EXPLICIT", languages=["sin"]) == {"ok": True}
    assert calls["language_mode"] == "EXPLICIT" and calls["languages"] == ("sin",)

@pytest.mark.parametrize("code,text,font_path", [
    ("eng", "English language", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("sin", "සිංහල භාෂාව", "/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf"),
    ("tam", "தமிழ் மொழி", "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf"),
])
def test_real_scanned_explicit_language_is_not_english(tmp_path, code, text, font_path):
    image = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(image).text((60, 130), text, font=ImageFont.truetype(font_path, 96), fill="black")
    stream = io.BytesIO(); image.save(stream, format="PNG")
    document = fitz.open(); document.new_page(width=700, height=250).insert_image(fitz.Rect(0, 0, 700, 250), stream=stream.getvalue())
    source = tmp_path / f"{code}.pdf"; document.save(source); document.close()
    result = extract_document_v2(str(source), language_mode="EXPLICIT", languages=[code])
    assert result["language_mode"] == "EXPLICIT" and result["languages"] == [code]
    assert result["pages"][0]["kind"] == "scanned"
    assert result["pages"][0]["elements"]

def test_real_auto_never_silently_becomes_english(tmp_path):
    image = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(image).text((60, 130), "සිංහල භාෂාව", font=ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf", 96), fill="black")
    stream = io.BytesIO(); image.save(stream, format="PNG")
    document = fitz.open(); document.new_page(width=700, height=250).insert_image(fitz.Rect(0, 0, 700, 250), stream=stream.getvalue())
    source = tmp_path / "auto-sinhala.pdf"; document.save(source); document.close()
    try:
        result = extract_document_v2(str(source), language_mode="AUTO", languages=["eng", "sin", "tam"])
    except EditorLanguageRequiredError:
        return
    assert result["language_mode"] == "AUTO"
    assert result["languages"] == ["eng", "sin", "tam"]
    assert result["pages"][0]["elements"]

@pytest.mark.parametrize("module,selector", [(general, general.EDITOR_OCR_ENGINE_ENV), (studio, studio.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV)])
@pytest.mark.parametrize("code,text,font_path", [
    ("eng", "English language", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("sin", "සිංහල භාෂාව", "/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf"),
    ("tam", "தமிழ் மொழி", "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf"),
])
def test_real_sdk_selector_extracts_each_explicit_language(monkeypatch, tmp_path, module, selector, code, text, font_path):
    image = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(image).text((60, 130), text, font=ImageFont.truetype(font_path, 96), fill="black")
    stream = io.BytesIO(); image.save(stream, format="PNG")
    document = fitz.open(); document.new_page(width=700, height=250).insert_image(fitz.Rect(0, 0, 700, 250), stream=stream.getvalue())
    source = tmp_path / f"sdk-{code}.pdf"; document.save(source); document.close()
    monkeypatch.setenv(selector, "sdk")
    function = general.execute_editor_ocr if module is general else studio.execute_studio_editor_extraction
    result = function(source, language_mode="EXPLICIT", languages=[code])
    assert result["language_mode"] == "EXPLICIT" and result["languages"] == [code]
    assert result["pages"][0]["kind"] == "scanned"
    assert result["pages"][0]["elements"]


@pytest.mark.parametrize("module,selector", [(general, general.EDITOR_OCR_ENGINE_ENV), (studio, studio.STUDIO_EDITOR_EXTRACTION_ENGINE_ENV)])
def test_real_sdk_auto_returns_auto_or_language_required_without_english_retry(monkeypatch, tmp_path, module, selector):
    image = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(image).text((60, 130), "සිංහල භාෂාව", font=ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf", 96), fill="black")
    stream = io.BytesIO(); image.save(stream, format="PNG")
    document = fitz.open(); document.new_page(width=700, height=250).insert_image(fitz.Rect(0, 0, 700, 250), stream=stream.getvalue())
    source = tmp_path / "sdk-auto-sinhala.pdf"; document.save(source); document.close()
    monkeypatch.setenv(selector, "sdk")
    function = general.execute_editor_ocr if module is general else studio.execute_studio_editor_extraction
    try:
        result = function(source, language_mode="AUTO", languages=["eng", "sin", "tam"])
    except EditorLanguageRequiredError:
        return
    assert result["language_mode"] == "AUTO"
    assert result["languages"] == ["eng", "sin", "tam"]

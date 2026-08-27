import fitz
import pytest

from app.api.tools.markup import document


@pytest.mark.parametrize("action", ["highlight", "underline", "strikeout"])
@pytest.mark.parametrize("mode", ["smart", "ocr"])
def test_text_aware_modes_render_each_markup_action(monkeypatch, action, mode):
    source = fitz.open()
    page = source.new_page(width=300, height=300)
    page.insert_text((10, 30), "Alpha Bravo", fontsize=18)
    words = [{"rect": fitz.Rect(10, 10, 80, 30), "text": "Alpha"}]
    if mode == "smart":
        monkeypatch.setattr(document, "native_words_in_rect", lambda *_: words)
    else:
        monkeypatch.setattr(document, "ocr_words_for_page", lambda *_: (words, None))

    document.apply_markup(source, [{"x": 0, "y": 0, "width": 120, "height": 60, "page": 1, "color": "#800080"}], action=action, mode=mode)
    assert source[0].get_drawings(), f"{action} {mode} produced no rendered mark"
    source.close()


def test_smart_prefers_native_words_without_ocr(monkeypatch):
    page = fitz.open().new_page(width=300, height=300)
    native = [{"rect": fitz.Rect(10, 10, 80, 24), "text": "native"}]
    monkeypatch.setattr(document, "native_words_in_rect", lambda *_: native)
    monkeypatch.setattr(document, "ocr_words_for_page", lambda *_: (_ for _ in ()).throw(AssertionError("OCR must not run when native text exists")))

    assert document._selection_word_items(page, fitz.Rect(0, 0, 100, 100), "smart") == native


def test_smart_falls_back_to_ocr_when_native_selection_is_empty(monkeypatch):
    page = fitz.open().new_page(width=300, height=300)
    ocr = [{"rect": fitz.Rect(10, 10, 80, 24), "text": "scanned"}, {"rect": fitz.Rect(200, 200, 220, 220), "text": "outside"}]
    monkeypatch.setattr(document, "native_words_in_rect", lambda *_: [])
    monkeypatch.setattr(document, "ocr_words_for_page", lambda *_: (ocr, None))

    selected = document._selection_word_items(page, fitz.Rect(0, 0, 100, 100), "smart")
    assert selected == [ocr[0]]


def test_ocr_mode_is_explicit_and_does_not_use_native(monkeypatch):
    page = fitz.open().new_page(width=300, height=300)
    ocr = [{"rect": fitz.Rect(10, 10, 80, 24), "text": "scanned"}]
    monkeypatch.setattr(document, "native_words_in_rect", lambda *_: (_ for _ in ()).throw(AssertionError("OCR mode must not use native extraction")))
    monkeypatch.setattr(document, "ocr_words_for_page", lambda *_: (ocr, None))

    assert document._selection_word_items(page, fitz.Rect(0, 0, 100, 100), "ocr") == ocr


def test_no_text_selection_completes_without_processor_error():
    source = fitz.open()
    source.new_page(width=300, height=300)
    document.apply_markup(
        source,
        [{"x": 20, "y": 20, "width": 100, "height": 40, "page": 1, "color": "#800080"}],
        action="highlight",
        mode="ocr",
    )
    assert source.page_count == 1
    source.close()

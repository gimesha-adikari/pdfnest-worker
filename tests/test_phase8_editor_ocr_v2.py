from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from app.api.tools.editor.document import extract_document_v2


def _scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (900, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
    draw.text((80, 150), "Editor OCR Alpha 42", font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    document = fitz.open()
    document.new_page(width=432, height=240).insert_image(fitz.Rect(0, 0, 432, 240), stream=stream.getvalue())
    document.save(path)
    document.close()


def test_editor_v2_adapts_scanned_canonical_words_without_direct_ocr_contract(tmp_path: Path) -> None:
    source = tmp_path / "scanned-editor.pdf"
    _scanned_pdf(source)

    result = extract_document_v2(str(source))

    page = result["pages"][0]
    assert page["kind"] == "scanned"
    assert page["source"] == "OCR_RECOGNITION"
    assert page["word_count"] >= 3
    assert page["reading_order"]
    assert page["elements"]
    element = page["elements"][0]
    assert element["ocr_v2"] is True
    assert element["word_ids"]
    assert len(element["word_geometry"]) == len(element["word_ids"])


def test_editor_v2_keeps_native_pages_native(tmp_path: Path) -> None:
    source = tmp_path / "native-editor.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((30, 80), "Native Editor Text", fontsize=20)
    document.save(source)
    document.close()

    result = extract_document_v2(str(source))

    page = result["pages"][0]
    assert page["kind"] == "text"
    assert page["source"] == "NATIVE_EXTRACTION"
    assert page["word_count"] >= 3

from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from app.api.tools.ocr.document import extract_text_from_pdf


def _scan_pdf(path: Path, text: str) -> None:
    image = Image.new("RGB", (1600, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 58)
    draw.text((60, 140), text, fill="black", font=font)
    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG")

    document = fitz.open()
    page = document.new_page(width=800, height=200)
    page.insert_image(page.rect, stream=image_bytes.getvalue())
    document.save(path)
    document.close()


def test_explicit_language_pdf_to_text_uses_native_first_ocr_v2(tmp_path: Path):
    native_pdf = tmp_path / "native.pdf"
    native_doc = fitz.open()
    native_doc.new_page().insert_text((72, 72), "Native canonical text")
    native_doc.save(native_pdf)
    native_doc.close()

    native_output = tmp_path / "native.txt"
    extract_text_from_pdf(str(native_pdf), str(native_output), lang="eng")
    assert "Native canonical text" in native_output.read_text(encoding="utf-8")

    scanned_pdf = tmp_path / "scanned.pdf"
    _scan_pdf(scanned_pdf, "Scanned OCR V2 text")
    scanned_output = tmp_path / "scanned.txt"
    extract_text_from_pdf(str(scanned_pdf), str(scanned_output), lang="eng")
    extracted = scanned_output.read_text(encoding="utf-8")
    assert "Scanned" in extracted
    assert "OCR" in extracted

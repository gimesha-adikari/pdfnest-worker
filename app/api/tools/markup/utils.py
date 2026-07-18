from __future__ import annotations

from typing import Optional

import fitz  # PyMuPDF


def normalize_hex(hex_color: str, default: str = "#FFFF00") -> tuple[float, float, float]:
    s = (hex_color or default).strip().lstrip("#")
    if len(s) != 6:
        s = default.lstrip("#")

    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return (r, g, b)


def open_document(input_path: str, password: Optional[str] = None) -> fitz.Document:
    doc = fitz.open(input_path)
    if doc.needs_pass:
        if not password:
            raise RuntimeError("PDF is password protected but no password was provided")
        if doc.authenticate(password) <= 0:
            raise RuntimeError("Invalid PDF password")
    return doc


def native_words_in_rect(page: fitz.Page, selection_rect: fitz.Rect) -> list[dict]:
    words: list[dict] = []
    for item in page.get_text("words"):
        if len(item) < 5:
            continue
        rect = fitz.Rect(item[0], item[1], item[2], item[3])
        text = str(item[4]).strip()
        if text and rect.intersects(selection_rect):
            words.append({"rect": rect, "text": text})
    return words
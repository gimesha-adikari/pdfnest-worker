from __future__ import annotations

from typing import Optional

import fitz


def open_document(input_path: str, password: Optional[str] = None) -> fitz.Document:
    doc = fitz.open(input_path)

    if doc.needs_pass:
        if not password:
            raise RuntimeError(
                "PDF is password protected but no password was provided"
            )

        if doc.authenticate(password) <= 0:
            raise RuntimeError("Invalid PDF password")

    return doc


def rect_area(bbox) -> float:
    try:
        r = fitz.Rect(bbox)
        return max(0.0, r.width * r.height)
    except Exception:
        return 0.0
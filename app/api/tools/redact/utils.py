from __future__ import annotations

import fitz


def open_document(path: str, password: str | None = None) -> fitz.Document:
    doc = fitz.open(path)

    if doc.needs_pass:
        if not password:
            doc.close()
            raise RuntimeError("Password required for encrypted PDF.")

        if doc.authenticate(password) <= 0:
            doc.close()
            raise RuntimeError("Invalid PDF password.")

    return doc
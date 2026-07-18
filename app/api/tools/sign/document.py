from __future__ import annotations

from pathlib import Path
from typing import Sequence

import fitz

from .models import SignatureStamp


def sign_pdf(
        input_pdf: str,
        signature_image: str,
        output_pdf: str,
        stamps: Sequence[SignatureStamp],
):
    doc = fitz.open(input_pdf)

    try:
        for stamp in stamps:

            page_index = stamp.page - 1

            if page_index < 0 or page_index >= len(doc):
                continue

            page = doc[page_index]

            rect = fitz.Rect(
                stamp.x,
                stamp.y,
                stamp.x + stamp.width,
                stamp.y + stamp.height,
                )

            page.insert_image(
                rect,
                filename=str(Path(signature_image)),
                overlay=True,
            )

        doc.save(
            output_pdf,
            garbage=4,
            clean=True,
            deflate=True,
        )

    finally:
        doc.close()
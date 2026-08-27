from __future__ import annotations

import math
from typing import Sequence

import fitz

from .models import RedactBox
from .utils import open_document


def redact_pdf(
        input_path: str,
        output_path: str,
        keywords: Sequence[str],
        boxes: Sequence[RedactBox],
):
    doc = open_document(input_path)

    try:
        if len(boxes) > 256:
            raise ValueError("too many redaction boxes")
        for box in boxes:
            values = (box.x, box.y, box.width, box.height)
            if not all(math.isfinite(value) for value in values):
                raise ValueError("redaction box geometry must be finite")
            if box.page < 1 or box.x < 0 or box.y < 0 or box.width <= 0 or box.height <= 0 or box.x + box.width > 1 or box.y + box.height > 1:
                raise ValueError("redaction box must be normalized and within the page")

        for page in doc:

            page_number = page.number + 1

            for keyword in keywords:
                keyword = keyword.strip()

                if not keyword:
                    continue

                for rect in page.search_for(keyword):
                    page.add_redact_annot(
                        rect,
                        fill=(0, 0, 0),
                    )

            for box in boxes:

                if box.page != page_number:
                    continue

                # Boxes arrive in visible, crop-relative normalized space.
                # Redaction annotations must be placed in the unrotated PDF
                # context, matching the shared Studio geometry contract.
                page_rotation = page.rotation
                visible_page_rect = page.rect
                derotation = page.derotation_matrix if page_rotation else None
                if page_rotation:
                    page.set_rotation(0)
                rect = fitz.Rect(
                    box.x * visible_page_rect.width,
                    box.y * visible_page_rect.height,
                    (box.x + box.width) * visible_page_rect.width,
                    (box.y + box.height) * visible_page_rect.height,
                )
                if derotation is not None:
                    rect *= derotation

                page.add_redact_annot(
                    rect,
                    fill=(0, 0, 0),
                )

                if page_rotation:
                    page.set_rotation(page_rotation)

            page.apply_redactions()

        doc.save(
            output_path,
            garbage=4,
            deflate=True,
            clean=True,
        )

    finally:
        doc.close()

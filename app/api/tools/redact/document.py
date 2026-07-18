from __future__ import annotations

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
        for page in doc:

            page_number = page.number + 1

            #
            # Keyword redactions
            #
            for keyword in keywords:
                keyword = keyword.strip()

                if not keyword:
                    continue

                for rect in page.search_for(keyword):
                    page.add_redact_annot(
                        rect,
                        fill=(0, 0, 0),
                    )

            #
            # Manual rectangles
            #
            page_rect = page.rect

            page_width = page_rect.width
            page_height = page_rect.height

            for box in boxes:

                if box.page != page_number:
                    continue

                rect = fitz.Rect(
                    box.x * page_width,
                    box.y * page_height,
                    (box.x + box.width) * page_width,
                    (box.y + box.height) * page_height,
                    )

                page.add_redact_annot(
                    rect,
                    fill=(0, 0, 0),
                )

            page.apply_redactions()

        doc.save(
            output_path,
            garbage=4,
            deflate=True,
            clean=True,
        )

    finally:
        doc.close()
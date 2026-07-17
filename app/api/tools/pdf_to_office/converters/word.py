from __future__ import annotations

import fitz
from docx import Document
from PIL import Image
import pytesseract


def convert_to_word(pdf_path: str, output_path: str) -> None:
    doc = fitz.open(pdf_path)
    try:
        total_text = sum(len(page.get_text().strip()) for page in doc)

        if total_text < (50 * len(doc)):
            doc_out = Document()
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                text = pytesseract.image_to_string(img)
                doc_out.add_paragraph(text)
                doc_out.add_page_break()

            doc_out.save(output_path)
        else:
            from pdf2docx import Converter

            cv = Converter(pdf_path)
            try:
                cv.convert(
                    output_path,
                    start=0,
                    end=None,
                    keep_page_layout=False,
                    connected_border=True,
                    line_overlap_margin=0.2,
                    line_margin=0.2,
                    word_margin=0.2,
                    bottom_margin=5.0,
                )
            finally:
                cv.close()
    finally:
        doc.close()

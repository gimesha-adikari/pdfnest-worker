from __future__ import annotations

import os
import sys
import tempfile
import subprocess
import fitz
from docx import Document
from PIL import Image
import pytesseract

from app.core.tesseract_capacity import acquire_tesseract_capacity


def _get_pdf2docx_worker_count() -> int:
    cpu_count = os.cpu_count() or 1
    return min(2, max(1, cpu_count))


def _run_pdf2docx_isolated(pdf_path: str, output_path: str, workers: int) -> None:
    abs_pdf = os.path.abspath(pdf_path)
    abs_output = os.path.abspath(output_path)

    use_mp = workers > 1

    with tempfile.TemporaryDirectory(prefix="pdf2docx-job-") as job_dir:
        py_script = (
            "from pdf2docx import Converter\n"
            f"cv = Converter({abs_pdf!r})\n"
            "try:\n"
            "    kwargs = {\n"
            "        'keep_page_layout': False,\n"
            "        'connected_border': True,\n"
            "        'line_overlap_margin': 0.2,\n"
            "        'line_margin': 0.2,\n"
            "        'word_margin': 0.2,\n"
            "        'bottom_margin': 5.0,\n"
            f"        'multi_processing': {use_mp!r},\n"
            f"        'cpu_count': {workers!r},\n"
            "    }\n"
            f"    cv.convert({abs_output!r}, start=0, end=None, **kwargs)\n"
            "finally:\n"
            "    cv.close()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", py_script],
            cwd=job_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"PDF to Word conversion failed ({proc.returncode}): {err_msg}")


def convert_to_word(pdf_path: str, output_path: str) -> None:
    doc = fitz.open(pdf_path)
    try:
        total_text = sum(len(page.get_text().strip()) for page in doc)

        if total_text < (50 * len(doc)):
            doc_out = Document()
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                with acquire_tesseract_capacity():
                    text = pytesseract.image_to_string(img)
                doc_out.add_paragraph(text)
                doc_out.add_page_break()

            doc_out.save(output_path)
        else:
            workers = _get_pdf2docx_worker_count()
            _run_pdf2docx_isolated(pdf_path, output_path, workers)
    finally:
        doc.close()

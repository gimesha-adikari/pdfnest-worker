import os
import tempfile
import fitz
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from app.api.tools.pdf_to_office.converters.word import (
    _get_pdf2docx_worker_count,
    convert_to_word,
)


def create_sample_text_pdf(path: str, pages: int = 5) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((50, 50), f"Page {i+1} Sample Text Content for PDF to Word Parallelism Unit Test.\n" * 5)
    doc.save(path)
    doc.close()


def test_get_pdf2docx_worker_count():
    with patch("os.cpu_count", return_value=1):
        assert _get_pdf2docx_worker_count() == 1

    with patch("os.cpu_count", return_value=2):
        assert _get_pdf2docx_worker_count() == 2

    with patch("os.cpu_count", return_value=8):
        assert _get_pdf2docx_worker_count() == 2

    with patch("os.cpu_count", return_value=None):
        assert _get_pdf2docx_worker_count() == 1


def test_convert_to_word_isolated_execution():
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "test.pdf")
        docx_path = os.path.join(tmpdir, "test.docx")
        create_sample_text_pdf(pdf_path, pages=3)

        initial_files = set(os.listdir(os.getcwd()))
        convert_to_word(pdf_path, docx_path)

        assert os.path.exists(docx_path)
        assert os.path.getsize(docx_path) > 0
        final_files = set(os.listdir(os.getcwd()))
        # Verify no intermediate pages-*.json files leaked into CWD
        assert final_files == initial_files


def test_concurrent_pdf_to_word_no_cwd_collision():
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf1 = os.path.join(tmpdir, "doc1.pdf")
        pdf2 = os.path.join(tmpdir, "doc2.pdf")
        docx1 = os.path.join(tmpdir, "out1.docx")
        docx2 = os.path.join(tmpdir, "out2.docx")

        create_sample_text_pdf(pdf1, pages=5)
        create_sample_text_pdf(pdf2, pages=5)

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(convert_to_word, pdf1, docx1)
            f2 = executor.submit(convert_to_word, pdf2, docx2)
            f1.result()
            f2.result()

        assert os.path.exists(docx1) and os.path.getsize(docx1) > 0
        assert os.path.exists(docx2) and os.path.getsize(docx2) > 0

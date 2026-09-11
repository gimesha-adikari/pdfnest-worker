from __future__ import annotations

import math
import os
import sys
import tempfile
from typing import Any
import fitz
from docx import Document

from app.core.ocr_v2.native import NativeDecision, NativeExtractor, NativeValidator
from app.core.subprocess_runner import run_hardened_subprocess
from app.core.pdf_to_word_ocr_engine import (
    configured_pdf_to_word_ocr_engine,
    execute_pdf_to_word_ocr,
)


PDF_TO_WORD_DEFAULT_RASTER_DPI = 200
PDF_TO_WORD_TIMEOUT_SECONDS = 300
# The SDK's structured pixel guard runs after PyMuPDF and Pillow have already
# materialized the raster.  Keep this PDF-to-Word-only preflight below the
# worker's 1 GiB production ceiling, leaving room for the service and OCR
# runtime around the raster itself.
PDF_TO_WORD_MAX_RASTER_PIXELS = 10_000_000
_FULL_PAGE_IMAGE_AREA_RATIO = 0.95


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
        proc = run_hardened_subprocess(
            [sys.executable, "-c", py_script],
            cwd=job_dir,
            timeout=PDF_TO_WORD_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip()
            raise RuntimeError(f"PDF to Word conversion failed ({proc.returncode}): {err_msg}")


def _requires_structured_ocr(pdf_path: str) -> bool:
    """Select the structured OCR route only for scanned or mixed pages.

    Classification is native-only and therefore does not invoke OCR.  The
    structured processor then performs the one native-first OCR V2 pass for
    pages that actually need it.
    """
    extractor = NativeExtractor()
    validator = NativeValidator()
    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            candidate = extractor.extract(page, page_index)
            decision = validator.validate(candidate)
            if decision.decision != NativeDecision.TRUST_NATIVE:
                return True
    return False


def _raster_pixel_count(page: Any, dpi: int) -> int:
    scale = dpi / 72.0
    width = max(1, math.ceil(float(page.rect.width) * scale))
    height = max(1, math.ceil(float(page.rect.height) * scale))
    return width * height


def _max_safe_page_raster_dpi(page: Any) -> int:
    """Find the highest pre-render DPI within the PDF-to-Word pixel budget."""

    if _raster_pixel_count(page, 1) > PDF_TO_WORD_MAX_RASTER_PIXELS:
        raise ValueError("PDF-to-Word page is too large for safe rasterization")
    if _raster_pixel_count(page, PDF_TO_WORD_DEFAULT_RASTER_DPI) <= PDF_TO_WORD_MAX_RASTER_PIXELS:
        return PDF_TO_WORD_DEFAULT_RASTER_DPI

    lower, upper = 1, PDF_TO_WORD_DEFAULT_RASTER_DPI
    while lower < upper:
        candidate = (lower + upper + 1) // 2
        if _raster_pixel_count(page, candidate) <= PDF_TO_WORD_MAX_RASTER_PIXELS:
            lower = candidate
        else:
            upper = candidate - 1
    return lower


def _full_page_image_source_dpi(page: Any) -> float | None:
    """Return the lowest intrinsic DPI of an image covering almost the page."""

    page_area = float(page.rect.width) * float(page.rect.height)
    if page_area <= 0:
        return None

    source_dpi: float | None = None
    for image in page.get_images(full=True):
        image_width, image_height = int(image[2]), int(image[3])
        if image_width <= 0 or image_height <= 0:
            continue
        for image_rect in page.get_image_rects(image):
            image_area = float(image_rect.width) * float(image_rect.height)
            if image_area / page_area < _FULL_PAGE_IMAGE_AREA_RATIO:
                continue
            dpi = min(
                image_width * 72.0 / float(image_rect.width),
                image_height * 72.0 / float(image_rect.height),
            )
            if dpi > 0 and (source_dpi is None or dpi < source_dpi):
                source_dpi = dpi
    return source_dpi


def _page_pdf_to_word_raster_dpi(page: Any) -> int:
    page_dpi = _max_safe_page_raster_dpi(page)
    if page_dpi < PDF_TO_WORD_DEFAULT_RASTER_DPI:
        source_dpi = _full_page_image_source_dpi(page)
        if source_dpi is not None:
            page_dpi = min(page_dpi, max(1, math.floor(source_dpi)))
    return page_dpi


def _get_pdf_to_word_raster_dpis(pdf_path: str) -> tuple[int, ...]:
    """Return the safe target DPI for each structured-OCR PDF page."""

    with fitz.open(pdf_path) as doc:
        return tuple(_page_pdf_to_word_raster_dpi(page) for page in doc)


def _get_pdf_to_word_raster_dpi(pdf_path: str) -> int | None:
    """Return a backward-compatible scalar summary of page-scoped preflight.

    Ordinary pages retain the existing 200 DPI behavior.  Only pages whose
    default render exceeds the PDF-to-Word safety budget are reduced.  For a
    full-page scan, the inferred embedded image resolution is also used as an upper
    bound so a low-DPI scan is not needlessly upsampled into a large raster.
    The converter uses ``_get_pdf_to_word_raster_dpis`` so unrelated pages do
    not inherit this scalar minimum.  ``None`` means that the selected engine
    should use its normal default.
    """

    selected_dpi = min(_get_pdf_to_word_raster_dpis(pdf_path), default=PDF_TO_WORD_DEFAULT_RASTER_DPI)
    return selected_dpi if selected_dpi < PDF_TO_WORD_DEFAULT_RASTER_DPI else None


def _structured_element_type(element: Any) -> str:
    """Read the canonical element value across internal and SDK enum types."""

    return str(getattr(getattr(element, "type", None), "value", getattr(element, "type", ""))).upper()


def _add_structured_element(doc: Document, element: Any) -> None:
    """Map only structure represented by the canonical structured result."""
    element_type = _structured_element_type(element)
    if element_type == "HEADING":
        doc.add_heading(element.text, level=max(1, min(9, element.level or 1)))
    elif element_type in {"PARAGRAPH", "TEXT_BLOCK"}:
        if element.text:
            doc.add_paragraph(element.text)
    elif element_type == "LIST":
        items = element.data.get("items", [])
        style = "List Number" if element.ordered else "List Bullet"
        for item in items:
            text = str(item.get("text", "")).strip()
            if text:
                doc.add_paragraph(text, style=style)
    elif element_type == "TABLE":
        headers = element.data.get("headers", [])
        rows = element.data.get("rows", [])
        table_rows = ([headers] if headers else []) + list(rows)
        column_count = max((len(row) for row in table_rows), default=0)
        if column_count:
            table = doc.add_table(rows=0, cols=column_count)
            table.style = "Table Grid"
            for row in table_rows:
                cells = table.add_row().cells
                for index, cell in enumerate(row):
                    cells[index].text = str(cell.get("text", ""))
    elif element_type == "CAPTION":
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(element.text)
        run.italic = True
    elif element_type == "FORMULA":
        # Only genuine structured formula text reaches this mapper.  Current
        # local Tesseract structured output deliberately does not fabricate it.
        if element.text:
            doc.add_paragraph(element.text)


def _write_structured_result_to_word(result: Any, output_path: str) -> None:
    doc_out = Document()
    for page_index, page in enumerate(result.pages):
        elements = {element.element_id: element for element in page.elements}
        for element_id in page.reading_order:
            element = elements[element_id]
            _add_structured_element(doc_out, element)
        if page_index < len(result.pages) - 1:
            doc_out.add_page_break()
    doc_out.save(output_path)


def _convert_structured_to_word(
    pdf_path: str,
    output_path: str,
    language: str,
    *,
    raster_dpi: int | None = None,
    raster_dpis: tuple[int, ...] | None = None,
) -> None:
    if raster_dpi is not None and raster_dpis is not None:
        raise ValueError("raster_dpi and raster_dpis are mutually exclusive")
    if raster_dpis is not None:
        result = execute_pdf_to_word_ocr(pdf_path, language=language, raster_dpis=raster_dpis)
    elif raster_dpi is None:
        result = execute_pdf_to_word_ocr(pdf_path, language=language)
    else:
        result = execute_pdf_to_word_ocr(pdf_path, language=language, raster_dpi=raster_dpi)
    _write_structured_result_to_word(result, output_path)


def convert_to_word(pdf_path: str, output_path: str, language: str = "eng") -> None:
    # Validate the consumer selector even when this document takes the native
    # route, so an invalid deployment configuration cannot be hidden by the
    # absence of an OCR fallback on a particular input.
    configured_pdf_to_word_ocr_engine()
    doc = fitz.open(pdf_path)
    try:
        structured = _requires_structured_ocr(pdf_path)
    finally:
        doc.close()

    if structured:
        raster_dpis = _get_pdf_to_word_raster_dpis(pdf_path)
        if len(set(raster_dpis)) == 1:
            raster_dpi = raster_dpis[0] if raster_dpis[0] < PDF_TO_WORD_DEFAULT_RASTER_DPI else None
            _convert_structured_to_word(pdf_path, output_path, language, raster_dpi=raster_dpi)
        else:
            _convert_structured_to_word(pdf_path, output_path, language, raster_dpis=raster_dpis)
        return

    workers = _get_pdf2docx_worker_count()
    _run_pdf2docx_isolated(pdf_path, output_path, workers)

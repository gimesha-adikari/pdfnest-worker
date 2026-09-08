from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
from docx import Document

from app.api.tools.pdf_to_office.converters.word import convert_to_word
from app.core.ocr_v2.structured import StructuredDocumentProcessor, StructuredElementType
from app.core.ocr_v2.table_ocr import detect_ruling_line_grid, prepare_table_ocr_image


_TABLE_ROWS = (
    ("001", "Alice", "10", "15", "12", "37"),
    ("002", "Bob", "8", "9", "11", "28"),
    ("003", "Charlie", "20", "18", "22", "60"),
    ("004", "Dave", "5", "5", "5", "15"),
)
_TABLE_HEADERS = ("ID", "Name", "Q1", "Q2", "Q3", "Total")
_OCR_HEADER_ALIASES = ({"ID"}, {"Name"}, {"Q1", "Ql"}, {"Q2"}, {"Q3"}, {"Total", "Tota1"})
_ALIGNMENT_TABLE_HEADERS = ("Name", "FName", "City", "Age", "Salary")
_ALIGNMENT_TABLE_ROWS = (
    ("Smith", "John", "3", "35", "$280"),
    ("Doe", "Jane", "1", "28", "$325"),
    ("Brown", "Scott", "3", "41", "$265"),
    ("Howard", "Shemp", "4", "48", "$359"),
    ("Taylor", "Tom", "2", "22", "$250"),
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)


def _centered_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.FreeTypeFont) -> None:
    left, top, right, bottom = box
    text_box = draw.textbbox((0, 0), text, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    draw.text(
        (
            left + (right - left - text_width) / 2 - text_box[0],
            top + (bottom - top - text_height) / 2 - text_box[1],
        ),
        text,
        fill="black",
        font=font,
    )


def _pdf_from_image(path: Path, image: Image.Image) -> None:
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    with fitz.open() as document:
        page = document.new_page(width=600, height=450)
        page.insert_image(page.rect, stream=encoded.getvalue())
        document.save(str(path))


def _scanned_table_pdf(path: Path) -> None:
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    x_edges = (80, 240, 500, 640, 780, 920, 1120)
    y_edges = (100, 220, 340, 460, 580, 700)
    for x in x_edges:
        draw.line((x, y_edges[0], x, y_edges[-1]), fill="black", width=5)
    for y in y_edges:
        draw.line((x_edges[0], y, x_edges[-1], y), fill="black", width=5)
    values = (_TABLE_HEADERS, *_TABLE_ROWS)
    for row, row_values in enumerate(values):
        for column, value in enumerate(row_values):
            _centered_text(draw, (x_edges[column] + 10, y_edges[row] + 10, x_edges[column + 1] - 10, y_edges[row + 1] - 10), value, _font(38))
    _pdf_from_image(path, image)


def _draw_aligned_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    right: bool,
) -> None:
    left, top, right_edge, bottom = box
    text_box = draw.textbbox((0, 0), text, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    x = right_edge - text_width - 10 - text_box[0] if right else left + 10 - text_box[0]
    y = top + (bottom - top - text_height) / 2 - text_box[1]
    draw.text((x, y), text, fill="black", font=font)


def _left_header_right_aligned_numeric_table_pdf(path: Path) -> None:
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    x_edges = (80, 300, 550, 700, 850, 1120)
    y_edges = (100, 220, 340, 460, 580, 700, 820)
    for x in x_edges:
        draw.line((x, y_edges[0], x, y_edges[-1]), fill="black", width=3)
    for y in y_edges:
        draw.line((x_edges[0], y, x_edges[-1], y), fill="black", width=3)

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 38)
    for column, value in enumerate(_ALIGNMENT_TABLE_HEADERS):
        _draw_aligned_cell(draw, (x_edges[column], y_edges[0], x_edges[column + 1], y_edges[1]), value, font, right=False)
    for row, row_values in enumerate(_ALIGNMENT_TABLE_ROWS, start=1):
        for column, value in enumerate(row_values):
            _draw_aligned_cell(
                draw,
                (x_edges[column], y_edges[row], x_edges[column + 1], y_edges[row + 1]),
                value,
                font,
                right=column >= 2,
            )
    _pdf_from_image(path, image)


def _scanned_columns_pdf(path: Path) -> None:
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    font = _font(28)
    left_lines = ("Left column text", "continues across several", "ordinary prose lines", "without a ruled grid.")
    right_lines = ("Right column text", "also has several lines", "but it is not a table", "and has no numeric rows.")
    for row, text in enumerate(left_lines):
        draw.text((60, 120 + row * 65), text, fill="black", font=font)
    for row, text in enumerate(right_lines):
        draw.text((650, 120 + row * 65), text, fill="black", font=font)
    _pdf_from_image(path, image)


def test_ruling_line_preparation_is_bounded_and_noop_without_grid() -> None:
    table_image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(table_image)
    for x in (80, 240, 500, 640, 780, 920, 1120):
        draw.line((x, 100, x, 700), fill="black", width=5)
    for y in (100, 220, 340, 460, 580, 700):
        draw.line((80, y, 1120, y), fill="black", width=5)

    grid = detect_ruling_line_grid(table_image)
    prepared, prepared_grid = prepare_table_ocr_image(table_image)
    assert len(grid.horizontal) == 6
    assert len(grid.vertical) == 7
    assert prepared_grid == grid
    assert prepared.size == table_image.size
    assert prepared.getpixel((80, 100)) == (255, 255, 255)

    plain = Image.new("RGB", (1200, 900), "white")
    unchanged, no_grid = prepare_table_ocr_image(plain)
    assert unchanged is plain
    assert no_grid is None


def test_pdf_to_word_scanned_table_recovers_six_columns_without_duplicate_paragraph(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scanned-table.pdf"
    _scanned_table_pdf(pdf_path)

    result = StructuredDocumentProcessor(
        raster_dpi=200,
        enable_scanned_table_recognition=True,
    ).process_document(pdf_path, routing_policy="FAST")

    elements = result.pages[0].elements
    tables = [element for element in elements if element.type is StructuredElementType.TABLE]
    assert result.validation["valid"] is True
    assert len(tables) == 1
    table = tables[0]
    assert table.data["column_count"] == 6
    assert table.data["row_count"] == 4
    assert [cell["text"] in aliases for cell, aliases in zip(table.data["headers"], _OCR_HEADER_ALIASES)] == [True] * len(_TABLE_HEADERS)
    assert [[cell["text"] for cell in row] for row in table.data["rows"]] == [list(row) for row in _TABLE_ROWS]
    assert table.bbox["x"] >= 0
    assert table.bbox["y"] >= 0
    assert table.bbox["width"] > 0
    assert table.bbox["height"] > 0
    assert not any(element.type is StructuredElementType.PARAGRAPH for element in elements)


def test_scanned_table_preserves_textual_header_over_right_aligned_numeric_cells(tmp_path: Path) -> None:
    pdf_path = tmp_path / "alignment-table.pdf"
    _left_header_right_aligned_numeric_table_pdf(pdf_path)

    result = StructuredDocumentProcessor(
        raster_dpi=200,
        enable_scanned_table_recognition=True,
    ).process_document(pdf_path, routing_policy="FAST")

    elements = list(result.pages[0].elements)
    tables = [element for element in elements if element.type is StructuredElementType.TABLE]
    assert len(tables) == 1
    table = tables[0]
    assert table.data["row_count"] == 5
    assert table.data["column_count"] == 5
    assert [cell["text"] for cell in table.data["headers"]] == list(_ALIGNMENT_TABLE_HEADERS)
    assert [[cell["text"] for cell in row] for row in table.data["rows"]] == [list(row) for row in _ALIGNMENT_TABLE_ROWS]
    assert not any(
        element.type is StructuredElementType.PARAGRAPH
        and element.text == "Name FName City Age Salary"
        for element in elements
    )
    first_body_y = min(cell["bbox"]["y"] for cell in table.data["rows"][0] if cell["bbox"])
    assert table.bbox["y"] < first_body_y


def test_scanned_table_recognition_does_not_promote_unruled_columns(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scanned-columns.pdf"
    _scanned_columns_pdf(pdf_path)

    result = StructuredDocumentProcessor(
        raster_dpi=200,
        enable_scanned_table_recognition=True,
    ).process_document(pdf_path, routing_policy="FAST")

    elements = result.pages[0].elements
    assert not any(element.type is StructuredElementType.TABLE for element in elements)
    assert any(element.type is StructuredElementType.PARAGRAPH for element in elements)


def test_pdf_to_word_projection_writes_recovered_table(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "scanned-table.pdf"
    docx_path = tmp_path / "scanned-table.docx"
    _scanned_table_pdf(pdf_path)
    monkeypatch.setenv("PDF_TO_WORD_OCR_ENGINE", "internal")

    convert_to_word(str(pdf_path), str(docx_path), language="eng")

    document = Document(docx_path)
    assert len(document.tables) == 1
    assert (len(document.tables[0].rows), len(document.tables[0].columns)) == (5, 6)
    assert [cell.text in aliases for cell, aliases in zip(document.tables[0].rows[0].cells, _OCR_HEADER_ALIASES)] == [True] * len(_TABLE_HEADERS)
    assert [cell.text for cell in document.tables[0].rows[-1].cells] == list(_TABLE_ROWS[-1])

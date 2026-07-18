from __future__ import annotations

from typing import Callable, Literal

import fitz

from app.api.tools.editor.document import group_words_by_line, ocr_words_for_page, sample_background_hex

from .utils import native_words_in_rect, normalize_hex, open_document


MarkupAction = Literal["highlight", "underline", "strikeout"]
MarkupMode = Literal["manual", "smart", "text", "ocr"]


def _draw_highlight_rect(page: fitz.Page, rect: fitz.Rect, color: tuple[float, float, float], opacity: float = 0.35) -> None:
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(color=None, fill=color, fill_opacity=opacity, width=0)
    shape.commit()


def _highlight_words(page: fitz.Page, word_items: list[dict], color: tuple[float, float, float]) -> None:
    lines = group_words_by_line(word_items)
    for line in lines:
        rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
        padding_x = max(0.8, rect.width * 0.02)
        padding_y = max(0.8, rect.height * 0.15)

        rect = fitz.Rect(
            max(0.0, rect.x0 - padding_x),
            max(0.0, rect.y0 - padding_y),
            min(page.rect.width, rect.x1 + padding_x),
            min(page.rect.height, rect.y1 + padding_y),
        )
        _draw_highlight_rect(page, rect, color, opacity=0.35)


def _draw_strike_line(page: fitz.Page, rect: fitz.Rect, color: tuple[float, float, float]) -> None:
    strike_y = rect.y0 + rect.height * 0.52
    thickness = max(0.8, min(2.5, rect.height * 0.12))

    page.draw_line(
        fitz.Point(rect.x0, strike_y),
        fitz.Point(rect.x1, strike_y),
        color=color,
        width=thickness,
    )


def _strike_words(page: fitz.Page, word_items: list[dict], color: tuple[float, float, float]) -> None:
    lines = group_words_by_line(word_items)
    for line in lines:
        rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
        _draw_strike_line(page, rect, color)


def _draw_manual_underline(page: fitz.Page, selection_rect: fitz.Rect, color: tuple[float, float, float]) -> None:
    thickness = max(0.8, min(2.5, selection_rect.height * 0.12))
    underline_y = min(page.rect.height - 1.0, selection_rect.y1 - max(1.0, selection_rect.height * 0.08))

    page.draw_line(
        fitz.Point(selection_rect.x0, underline_y),
        fitz.Point(selection_rect.x1, underline_y),
        color=color,
        width=thickness,
    )


def _underline_words(page: fitz.Page, word_items: list[dict], color: tuple[float, float, float]) -> None:
    lines = group_words_by_line(word_items)
    for line in lines:
        x0 = line["x0"]
        x1 = line["x1"]
        y1 = line["y1"]
        thickness = max(0.8, min(2.5, (y1 - line["y0"]) * 0.12))
        underline_y = min(page.rect.height - 1.0, y1 + 1.3)

        page.draw_line(
            fitz.Point(x0, underline_y),
            fitz.Point(x1, underline_y),
            color=color,
            width=thickness,
        )


def _selection_word_items(page: fitz.Page, selection_rect: fitz.Rect, mode: MarkupMode) -> list[dict]:
    mode = (mode or "smart").strip().lower()  # type: ignore[assignment]

    if mode in ("text", "smart"):
        native = native_words_in_rect(page, selection_rect)
        if native:
            return native
        return []

    if mode == "ocr":
        ocr_items = ocr_words_for_page(page)
        return [item for item in ocr_items if item["rect"].intersects(selection_rect)]

    return []


def apply_markup(
        doc: fitz.Document,
        boxes: list[dict],
        action: MarkupAction,
        mode: MarkupMode = "smart",
        progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    total = max(1, len(boxes))
    mode = (mode or "smart").strip().lower()  # type: ignore[assignment]

    for index, box in enumerate(boxes, start=1):
        page_num = int(box.get("page", 0))
        if page_num < 1 or page_num > doc.page_count:
            if progress_callback:
                progress_callback(index, total)
            continue

        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
        width = float(box.get("width", 0))
        height = float(box.get("height", 0))
        if width <= 0 or height <= 0:
            if progress_callback:
                progress_callback(index, total)
            continue

        page = doc[page_num - 1]
        selection_rect = fitz.Rect(x, y, x + width, y + height)
        color = normalize_hex(box.get("color", "#FFFF00"), "#FFFF00")

        if action == "highlight":
            if mode == "manual":
                _draw_highlight_rect(page, selection_rect, color, opacity=0.35)
            else:
                selected = _selection_word_items(page, selection_rect, mode)
                if selected:
                    _highlight_words(page, selected, color)
                else:
                    _draw_highlight_rect(page, selection_rect, color, opacity=0.35)

        elif action == "strikeout":
            if mode == "manual":
                _draw_strike_line(page, selection_rect, color)
            else:
                selected = _selection_word_items(page, selection_rect, mode)
                if selected:
                    _strike_words(page, selected, color)
                else:
                    _draw_strike_line(page, selection_rect, color)

        elif action == "underline":
            if mode == "manual":
                _draw_manual_underline(page, selection_rect, color)
            else:
                selected = _selection_word_items(page, selection_rect, mode)
                if selected:
                    _underline_words(page, selected, color)
                else:
                    _draw_manual_underline(page, selection_rect, color)

        if progress_callback:
            progress_callback(index, total)


def process_markup_pdf(
        input_path: str,
        output_path: str,
        boxes: list[dict],
        action: MarkupAction,
        mode: MarkupMode = "smart",
        password: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    doc = open_document(input_path, password)
    try:
        apply_markup(doc, boxes, action=action, mode=mode, progress_callback=progress_callback)
        doc.save(output_path, deflate=True, garbage=4)
    finally:
        doc.close()
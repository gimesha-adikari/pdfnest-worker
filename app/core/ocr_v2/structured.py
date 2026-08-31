"""Canonical structured-document extraction for OCR V2.

This module is deliberately engine-neutral. Native PDF structure is retained;
scanned pages may receive conservative headings, lists, and simple aligned
tables from existing Tesseract line/token geometry. Complex semantics remain
unsupported unless an upstream structured engine provides them.
"""

from __future__ import annotations

import time
import uuid
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pymupdf as fitz

from app.api.tools.markdown.extractor import (
    BULLET_REGEX,
    NUMBERED_LIST_REGEX,
    calculate_document_base_font_size,
    classify_heading_level,
    is_bold_font,
    is_italic_font,
)
from app.api.tools.markdown.table_extractor import extract_table_nodes_from_pdfplumber

from .contracts import (
    PageContentClassification,
    PageGeometry,
    PageProcessingSource,
    PageResult,
    ResultCapability,
)
from .geometry import PreparedRaster
from .orchestration import OCRV2Worker
from .adapters.tesseract import TesseractAdapter


STRUCTURED_SCHEMA_VERSION = "ocr_v2_structured_document.v1"
DEFAULT_STRUCTURED_MAX_INPUT_BYTES = 100 * 1024 * 1024
DEFAULT_STRUCTURED_MAX_OUTPUT_BYTES = 20 * 1024 * 1024
DEFAULT_STRUCTURED_MAX_PAGES = 150
DEFAULT_STRUCTURED_MAX_RASTER_PIXELS = 25_000_000
DEFAULT_STRUCTURED_PAGE_TIMEOUT_SECONDS = 120.0


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def structured_max_input_bytes() -> int:
    return _positive_env_int("OCR_V2_STRUCTURED_MAX_BYTES", DEFAULT_STRUCTURED_MAX_INPUT_BYTES)


def structured_max_output_bytes() -> int:
    return _positive_env_int("OCR_V2_STRUCTURED_MAX_OUTPUT_BYTES", DEFAULT_STRUCTURED_MAX_OUTPUT_BYTES)


def structured_max_pages() -> int:
    return _positive_env_int("OCR_V2_MAX_PAGES", DEFAULT_STRUCTURED_MAX_PAGES)


def structured_max_raster_pixels() -> int:
    return _positive_env_int("OCR_V2_STRUCTURED_MAX_RASTER_PIXELS", DEFAULT_STRUCTURED_MAX_RASTER_PIXELS)


def structured_page_timeout_seconds() -> float:
    try:
        value = float(os.getenv("OCR_V2_STRUCTURED_PAGE_TIMEOUT_SECONDS", str(DEFAULT_STRUCTURED_PAGE_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_STRUCTURED_PAGE_TIMEOUT_SECONDS
    return max(1.0, value)


class StructuredElementType(str, Enum):
    DOCUMENT = "DOCUMENT"
    PAGE = "PAGE"
    REGION = "REGION"
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    TEXT_BLOCK = "TEXT_BLOCK"
    LIST = "LIST"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"
    TABLE_ROW = "TABLE_ROW"
    TABLE_CELL = "TABLE_CELL"
    FORMULA = "FORMULA"
    IMAGE = "IMAGE"
    CAPTION = "CAPTION"


@dataclass(frozen=True)
class UnnormalizedStructuredPageOutput:
    """Structured-engine output before canonical normalization."""

    page_id: str
    elements: tuple[Mapping[str, Any], ...] = ()
    coordinate_space: str = "unknown"
    provenance: str = "structured-engine"
    raw_output: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class StructuredEngineAdapter(ABC):
    """Boundary for future structured engines such as a VLM parser."""

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        raise NotImplementedError


class TesseractStructuredAdapter(StructuredEngineAdapter):
    """Structured adapter backed by Tesseract TSV line/block output.

    It intentionally exposes text and geometry only.  Tables, formulas, and
    hierarchy are not fabricated from this adapter's plain OCR output.
    """

    def __init__(self, language: str) -> None:
        self.engine = TesseractAdapter(language)

    def describe(self) -> dict[str, Any]:
        return {
            "id": "tesseract_structured_v1",
            "source": "tesseract-tsv",
            "capabilities": ["TEXT", "LINE_GEOMETRY", "BLOCK_GEOMETRY", "READING_ORDER"],
        }

    def recognize_page(
        self,
        page_id: str,
        raster: PreparedRaster,
        page_geometry: PageGeometry,
        language: str,
        cancellation_check: Callable[[], None] | None = None,
        deadline: float | None = None,
    ) -> UnnormalizedStructuredPageOutput:
        if cancellation_check:
            cancellation_check()
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("structured OCR page deadline exceeded")
        output = self.engine.recognize_page(page_id, raster)
        return UnnormalizedStructuredPageOutput(
            page_id=page_id,
            elements=tuple(item for item in output.items if item.get("kind") == "line"),
            coordinate_space=output.coordinate_space,
            provenance="tesseract_structured_v1",
            raw_output=output.raw_output,
            metadata={"language": language, "page_geometry": page_geometry},
        )

@dataclass(frozen=True)
class StructuredElement:
    element_id: str
    type: StructuredElementType
    page_index: int
    text: str = ""
    bbox: dict[str, float] | None = None
    source: str = "unknown"
    confidence: float | None = None
    level: int | None = None
    ordered: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "element_id": self.element_id,
            "type": self.type.value,
            "page_index": self.page_index,
            "text": self.text,
            "source": self.source,
        }
        if self.bbox is not None:
            value["bbox"] = dict(self.bbox)
        if self.confidence is not None:
            value["confidence"] = self.confidence
        if self.level is not None:
            value["level"] = self.level
        if self.ordered is not None:
            value["ordered"] = self.ordered
        if self.data:
            value["data"] = self.data
        return value


@dataclass(frozen=True)
class StructuredPage:
    page_index: int
    page_id: str
    geometry: dict[str, Any]
    classification: str
    processing_source: str
    status: str
    elements: tuple[StructuredElement, ...]
    reading_order: tuple[str, ...]
    capabilities: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    language: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_id": self.page_id,
            "geometry": self.geometry,
            "classification": self.classification,
            "processing_source": self.processing_source,
            "status": self.status,
            "elements": [element.to_dict() for element in self.elements],
            "reading_order": list(self.reading_order),
            "capabilities": list(self.capabilities),
            "warnings": list(self.warnings),
            "language": self.language,
        }


@dataclass(frozen=True)
class StructuredDocumentResult:
    schema_version: str
    result_id: str
    source: dict[str, Any]
    elements: tuple[StructuredElement, ...]
    pages: tuple[StructuredPage, ...]
    capabilities: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    warnings: tuple[str, ...]
    validation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "source": self.source,
            "elements": [element.to_dict() for element in self.elements],
            "pages": [page.to_dict() for page in self.pages],
            "capabilities": list(self.capabilities),
            "available_capabilities": list(self.available_capabilities),
            "warnings": list(self.warnings),
            "validation": self.validation,
        }


def _bbox(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    return {"x": float(x0), "y": float(y0), "width": max(0.0, float(x1 - x0)), "height": max(0.0, float(y1 - y0))}


def _element_sort_key(element: StructuredElement) -> tuple[float, float, int, str]:
    bbox = (element.data.get("layout_bbox") if element.data else None) or element.bbox or {}
    return (float(bbox.get("y", 0.0)), float(bbox.get("x", 0.0)), element.page_index, element.element_id)


def _overlaps(a: dict[str, float] | None, b: dict[str, float] | None) -> bool:
    if not a or not b:
        return False
    ax1 = a["x"] + a["width"]
    ay1 = a["y"] + a["height"]
    bx1 = b["x"] + b["width"]
    by1 = b["y"] + b["height"]
    return not (ax1 <= b["x"] or bx1 <= a["x"] or ay1 <= b["y"] or by1 <= a["y"])


def _native_elements(page: fitz.Page, page_index: int, base_font_size: float) -> list[StructuredElement]:
    elements: list[StructuredElement] = []
    text_page = page.get_text("dict")
    for block_index, block in enumerate(text_page.get("blocks", [])):
        if block.get("type") != 0:
            continue
        raw_lines = block.get("lines", [])
        texts: list[str] = []
        max_size = 10.0
        bold = False
        italic = False
        spans_for_data: list[dict[str, Any]] = []
        for line in raw_lines:
            for span in line.get("spans", []):
                text = str(span.get("text", "")).strip()
                if not text:
                    continue
                texts.append(text)
                size = float(span.get("size", 10.0))
                max_size = max(max_size, size)
                font = str(span.get("font", ""))
                flags = int(span.get("flags", 0))
                bold = bold or is_bold_font(flags, font)
                italic = italic or is_italic_font(flags, font)
                spans_for_data.append({"text": text, "font_size": size, "bold": is_bold_font(flags, font), "italic": is_italic_font(flags, font)})
        text = " ".join(texts).strip()
        if not text:
            continue
        x0, y0, x1, y1 = block["bbox"]
        box = _bbox(x0, y0, x1, y1)
        heading = classify_heading_level(max_size, base_font_size, bold, text, is_isolated=True)
        element_type = StructuredElementType.HEADING if heading else StructuredElementType.PARAGRAPH
        ordered: bool | None = None
        data: dict[str, Any] = {"spans": spans_for_data}
        if BULLET_REGEX.match(text) or NUMBERED_LIST_REGEX.match(text):
            element_type = StructuredElementType.LIST
            ordered = bool(NUMBERED_LIST_REGEX.match(text))
            data["items"] = [{"item_index": 0, "text": text, "level": 0, "marker": "1." if ordered else "-"}]
        elements.append(StructuredElement(
            element_id=f"native-{page_index}-{block_index}",
            type=element_type,
            page_index=page_index,
            text=text,
            bbox=box,
            source="pymupdf_native",
            level=heading,
            ordered=ordered,
            data=data,
        ))
    return elements


def _ocr_elements(page_result: PageResult) -> list[StructuredElement]:
    elements: list[StructuredElement] = []
    for block in page_result.blocks:
        elements.append(StructuredElement(
            element_id=f"ocr-block-{page_result.page_index}-{block.id}",
            type=StructuredElementType.TEXT_BLOCK,
            page_index=page_result.page_index,
            text=block.text,
            bbox={"x": block.bbox.x, "y": block.bbox.y, "width": block.bbox.width, "height": block.bbox.height},
            source="tesseract_ocr",
            data={
                "line_ids": list(block.line_ids),
                "line_geometry_available": True,
                "word_geometry_available": bool(page_result.tokens),
            },
        ))
    if not elements and page_result.text.strip():
        elements.append(StructuredElement(
            element_id=f"ocr-text-{page_result.page_index}",
            type=StructuredElementType.TEXT_BLOCK,
            page_index=page_result.page_index,
            text=page_result.text.strip(),
            source="tesseract_ocr",
        ))
    return elements


@dataclass(frozen=True)
class _OCRLayoutLine:
    """One OCR line in a rotation-normalized layout space.

    Tesseract receives the rendered page image, while PDF text extraction uses
    the page's unrotated crop-box coordinates.  Keeping both boxes lets the
    structure heuristics reason about an upright page without changing the
    canonical source geometry stored in the result.
    """

    text: str
    source_bbox: dict[str, float]
    layout_bbox: dict[str, float]
    tokens: tuple[tuple[str, dict[str, float], dict[str, float]], ...]
    line_id: str


def _rotate_bbox_for_layout(bbox: dict[str, float], geometry: PageGeometry) -> dict[str, float]:
    """Map OCR coordinates into an upright layout space for structure analysis."""
    x, y, width, height = bbox["x"], bbox["y"], bbox["width"], bbox["height"]
    rotation = geometry.rotation % 360
    if rotation == 90:
        return {"x": y, "y": geometry.width - (x + width), "width": height, "height": width}
    if rotation == 180:
        return {
            "x": geometry.width - (x + width),
            "y": geometry.height - (y + height),
            "width": width,
            "height": height,
        }
    if rotation == 270:
        return {"x": geometry.height - (y + height), "y": x, "width": height, "height": width}
    return dict(bbox)


def _bbox_union(boxes: Sequence[dict[str, float]]) -> dict[str, float]:
    if not boxes:
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    x0 = min(box["x"] for box in boxes)
    y0 = min(box["y"] for box in boxes)
    x1 = max(box["x"] + box["width"] for box in boxes)
    y1 = max(box["y"] + box["height"] for box in boxes)
    return {"x": x0, "y": y0, "width": max(0.0, x1 - x0), "height": max(0.0, y1 - y0)}


def _ocr_layout_lines(page_result: PageResult) -> list[_OCRLayoutLine]:
    token_map = page_result.tokens_by_id
    lines: list[_OCRLayoutLine] = []
    for line in page_result.lines:
        text = line.text.strip()
        if not text:
            continue
        source_bbox = {"x": line.bbox.x, "y": line.bbox.y, "width": line.bbox.width, "height": line.bbox.height}
        token_values: list[tuple[str, dict[str, float], dict[str, float]]] = []
        for token_id in line.token_ids:
            token = token_map.get(token_id)
            if token is None or not token.text.strip():
                continue
            token_source_bbox = {"x": token.bbox.x, "y": token.bbox.y, "width": token.bbox.width, "height": token.bbox.height}
            token_values.append((token.text.strip(), token_source_bbox, _rotate_bbox_for_layout(token_source_bbox, page_result.geometry)))
        token_values.sort(key=lambda value: (value[2]["x"], value[2]["y"]))
        lines.append(_OCRLayoutLine(text, source_bbox, _rotate_bbox_for_layout(source_bbox, page_result.geometry), tuple(token_values), line.id))
    return sorted(lines, key=lambda line: (line.layout_bbox["y"], line.layout_bbox["x"]))


def _layout_dimensions(page_result: PageResult) -> tuple[float, float]:
    if page_result.geometry.rotation % 360 in {90, 270}:
        return page_result.geometry.height, page_result.geometry.width
    return page_result.geometry.width, page_result.geometry.height


def _line_gap(previous: _OCRLayoutLine, current: _OCRLayoutLine) -> float:
    return max(0.0, current.layout_bbox["y"] - (previous.layout_bbox["y"] + previous.layout_bbox["height"]))


def _line_content_bbox(line: _OCRLayoutLine) -> dict[str, float]:
    """Use token extent when OCR line boxes are coarse block-width rectangles."""
    if line.tokens:
        return _bbox_union([token[2] for token in line.tokens])
    return line.layout_bbox


def _median_line_height(lines: Sequence[_OCRLayoutLine]) -> float:
    heights = sorted(line.layout_bbox["height"] for line in lines if line.layout_bbox["height"] > 0)
    if not heights:
        return 8.0
    middle = len(heights) // 2
    return heights[middle] if len(heights) % 2 else (heights[middle - 1] + heights[middle]) / 2.0


def _is_linguistic_line(text: str) -> bool:
    letters = sum(character.isalpha() for character in text)
    visible = sum(not character.isspace() for character in text)
    return letters >= 5 and visible > 0 and (letters / visible) >= 0.55


def _ocr_heading_level(line: _OCRLayoutLine, index: int, lines: Sequence[_OCRLayoutLine], page_width: float, page_height: float, median_height: float) -> int | None:
    text = line.text.strip()
    if not _is_linguistic_line(text) or len(text) > 100 or len(text.split()) > 12:
        return None
    if text.endswith((".", ",", ";", ":", "?", "!")) or ":" in text:
        return None
    if line.layout_bbox["y"] > page_height * 0.92:
        return None

    letters = [character for character in text if character.isalpha()]
    upper_ratio = sum(character.isupper() for character in letters) / max(1, len(letters))
    words = [word for word in re.findall(r"[^\W\d_]+", text, flags=re.UNICODE) if word]
    word_count = len(text.split())
    title_case_words = sum(word[0].isupper() for word in words if word)
    title_like = len(words) >= 2 and title_case_words / len(words) >= 0.60
    content_bbox = _line_content_bbox(line)
    centered = abs((content_bbox["x"] + content_bbox["width"] / 2.0) - page_width / 2.0) <= page_width * 0.16
    previous_gap = _line_gap(lines[index - 1], line) if index else float("inf")
    next_gap = _line_gap(line, lines[index + 1]) if index + 1 < len(lines) else float("inf")
    isolated = max(previous_gap, next_gap) >= median_height * 1.15

    if upper_ratio >= 0.78 and ((centered and word_count <= 6) or (isolated and word_count <= 4)):
        return 1 if centered and len(text) <= 70 else 2
    title_connectors = {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to"}
    title_case_only = all(
        word.lower() in title_connectors or (word[0].isupper() and (len(word) == 1 or word[1:].islower()))
        for word in words
    )
    if title_like and title_case_only and centered and isolated and not any(character.isdigit() for character in text) and content_bbox["width"] <= page_width * 0.45:
        return 2
    return None


def _is_list_line(text: str) -> tuple[bool, bool, str]:
    clean = text.strip()
    if "|" in clean:
        return False, False, clean
    if BULLET_REGEX.match(clean):
        return True, False, BULLET_REGEX.sub("", clean, count=1).strip()
    if NUMBERED_LIST_REGEX.match(clean):
        return True, True, NUMBERED_LIST_REGEX.sub("", clean, count=1).strip()
    return False, False, clean


def _table_header_groups(line: _OCRLayoutLine) -> list[dict[str, Any]]:
    if len(line.tokens) < 3:
        return []
    sorted_tokens = list(line.tokens)
    span = max(1.0, line.layout_bbox["width"])
    break_gap = max(18.0, span * 0.05)
    groups: list[list[tuple[str, dict[str, float], dict[str, float]]]] = [[]]
    previous_right: float | None = None
    for token in sorted_tokens:
        token_left = token[2]["x"]
        if previous_right is not None and token_left - previous_right > break_gap:
            groups.append([])
        groups[-1].append(token)
        previous_right = max(previous_right or token[2]["x"], token[2]["x"] + token[2]["width"])
    if len(groups) < 3:
        return []
    return [{"text": " ".join(token[0] for token in group), "bbox": _bbox_union([token[1] for token in group]), "rowspan": 1, "colspan": 1} for group in groups]


def _simple_ocr_table(lines: Sequence[_OCRLayoutLine], page_result: PageResult, median_height: float) -> tuple[StructuredElement | None, set[str]]:
    """Recover only repeated, four-ish-column OCR rows with stable geometry."""
    row_candidates: list[_OCRLayoutLine] = []
    for line in lines:
        if len(line.tokens) < 3:
            continue
        # A column-header line can look like a short numbered row because
        # abbreviations such as "NO" are two characters long.  Keep headers
        # for the explicit header pass below, but never use them as data rows.
        alpha_values = [character for token in line.tokens for character in token[0] if character.isalpha()]
        header_like = (
            len(line.tokens) >= 3
            and not any(character.isdigit() for token in line.tokens for character in token[0])
            and bool(alpha_values)
            and sum(character.isupper() for character in alpha_values) / len(alpha_values) >= 0.75
        )
        if header_like:
            continue
        first = re.sub(r"[^\w]", "", line.tokens[0][0], flags=re.UNICODE)
        has_row_number = bool(re.search(r"\d", first)) or len(first) <= 2
        numeric_middle = [token for token in line.tokens[1:] if re.search(r"\d", token[0]) and token[2]["x"] > line.layout_bbox["x"] + line.layout_bbox["width"] * 0.58]
        tail = line.tokens[-1][0]
        has_status_tail = sum(character.isalpha() for character in tail) >= 4
        if has_row_number and (numeric_middle or has_status_tail or (len(line.tokens) >= 3 and any(character.isalpha() for token in line.tokens[1:] for character in token[0]))):
            row_candidates.append(line)
    if len(row_candidates) < 4:
        return None, set()
    row_candidates = sorted(row_candidates, key=lambda line: line.layout_bbox["y"])
    runs: list[list[_OCRLayoutLine]] = [[]]
    for line in row_candidates:
        if runs[-1] and line.layout_bbox["y"] - runs[-1][-1].layout_bbox["y"] > median_height * 3.0:
            runs.append([])
        runs[-1].append(line)
    row_candidates = max(runs, key=len)
    if len(row_candidates) < 4:
        return None, set()
    status_rows = sum(sum(character.isalpha() for character in line.tokens[-1][0]) >= 4 for line in row_candidates)
    if status_rows < 3:
        return None, set()

    first_row = row_candidates[0]
    preceding = [line for line in lines if line.layout_bbox["y"] < first_row.layout_bbox["y"] and first_row.layout_bbox["y"] - line.layout_bbox["y"] <= median_height * 3.0]
    header = max(preceding, key=lambda line: line.layout_bbox["y"], default=None)
    headers = _table_header_groups(header) if header else []
    if len(headers) < 3:
        return None, set()

    table_lines = [line for line in lines if first_row.layout_bbox["y"] - line.layout_bbox["y"] <= median_height * 3.0 and line.layout_bbox["y"] <= row_candidates[-1].layout_bbox["y"] + median_height * 1.5]
    table_left = min(line.layout_bbox["x"] for line in row_candidates)
    table_right = max(line.layout_bbox["x"] + line.layout_bbox["width"] for line in row_candidates)
    table_keys = {line.line_id for line in table_lines if line.layout_bbox["x"] <= table_right and line.layout_bbox["x"] + line.layout_bbox["width"] >= table_left}
    table_keys.update(line.line_id for line in row_candidates)

    def row_cells(line: _OCRLayoutLine) -> list[dict[str, Any]]:
        tokens = list(line.tokens)
        credits_index = max((index for index, token in enumerate(tokens[1:], start=1) if re.search(r"\d", token[0]) and token[2]["x"] > table_left + (table_right - table_left) * 0.58), default=-1)
        has_status_tail = sum(character.isalpha() for character in tokens[-1][0]) >= 4
        if credits_index <= 1:
            if has_status_tail:
                credits_index = len(tokens) - 1
                status_index = credits_index
            else:
                credits_index = len(tokens)
                status_index = credits_index
        else:
            status_index = credits_index + 1
        cells = [tokens[0:1], tokens[1:credits_index], tokens[credits_index:status_index], tokens[status_index:]]
        result: list[dict[str, Any]] = []
        for cell in cells:
            if not cell:
                result.append({"text": "", "bbox": None, "rowspan": 1, "colspan": 1})
            else:
                result.append({"text": " ".join(token[0] for token in cell), "bbox": _bbox_union([token[1] for token in cell]), "rowspan": 1, "colspan": 1})
        return result

    rows = [row_cells(line) for line in row_candidates]
    if any(len(row) != 4 or not row[1]["text"] for row in rows):
        return None, set()
    original_boxes = [line.source_bbox for line in table_lines]
    layout_boxes = [line.layout_bbox for line in table_lines]
    table = StructuredElement(
        element_id=f"ocr-table-{page_result.page_index}-0",
        type=StructuredElementType.TABLE,
        page_index=page_result.page_index,
        text="\n".join(" | ".join(cell["text"] for cell in row) for row in rows),
        bbox=_bbox_union(original_boxes),
        source="tesseract_ocr_structure",
        confidence=0.78,
        data={"headers": headers, "rows": rows, "row_count": len(rows), "column_count": 4, "reconstruction": "simple-aligned-ocr-table-v1", "layout_bbox": _bbox_union(layout_boxes)},
    )
    return table, table_keys


def _ocr_structured_elements(page_result: PageResult) -> list[StructuredElement]:
    lines = _ocr_layout_lines(page_result)
    if not lines:
        return _ocr_elements(page_result)
    page_width, page_height = _layout_dimensions(page_result)
    median_height = _median_line_height(lines)
    heading_levels = {line.line_id: _ocr_heading_level(line, index, lines, page_width, page_height, median_height) for index, line in enumerate(lines)}
    table, table_line_ids = _simple_ocr_table(lines, page_result, median_height)
    elements: list[StructuredElement] = []
    current: list[_OCRLayoutLine] = []

    def flush_paragraph() -> None:
        if not current:
            return
        elements.append(StructuredElement(
            element_id=f"ocr-paragraph-{page_result.page_index}-{len(elements)}",
            type=StructuredElementType.PARAGRAPH,
            page_index=page_result.page_index,
            text=" ".join(line.text for line in current),
            bbox=_bbox_union([line.source_bbox for line in current]),
            source="tesseract_ocr_structure",
            confidence=0.72,
            data={"line_ids": [line.line_id for line in current], "layout_geometry_available": True, "layout_bbox": _bbox_union([line.layout_bbox for line in current])},
        ))
        current.clear()

    def compatible(previous: _OCRLayoutLine, line: _OCRLayoutLine) -> bool:
        previous_box, box = previous.layout_bbox, line.layout_bbox
        previous_right = previous_box["x"] + previous_box["width"]
        right = box["x"] + box["width"]
        overlap = max(0.0, min(previous_right, right) - max(previous_box["x"], box["x"]))
        min_width = max(1.0, min(previous_box["width"], box["width"]))
        return abs(previous_box["x"] - box["x"]) <= max(12.0, page_width * 0.04) or overlap / min_width >= 0.35

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.line_id in table_line_ids:
            flush_paragraph()
            index += 1
            continue
        heading_level = heading_levels.get(line.line_id)
        if heading_level is not None:
            flush_paragraph()
            elements.append(StructuredElement(
                element_id=f"ocr-heading-{page_result.page_index}-{len(elements)}",
                type=StructuredElementType.HEADING,
                page_index=page_result.page_index,
                text=line.text,
                bbox=line.source_bbox,
                source="tesseract_ocr_structure",
                confidence=0.80,
                level=heading_level,
                data={"line_ids": [line.line_id], "layout_bbox": line.layout_bbox},
            ))
            index += 1
            continue
        is_list, ordered, list_text = _is_list_line(line.text)
        if is_list:
            flush_paragraph()
            list_lines = [line]
            list_values = [(ordered, list_text)]
            while index + 1 < len(lines):
                next_line = lines[index + 1]
                next_is_list, next_ordered, next_text = _is_list_line(next_line.text)
                if not next_is_list or next_ordered != ordered or _line_gap(list_lines[-1], next_line) > median_height * 2.5:
                    break
                list_lines.append(next_line)
                list_values.append((next_ordered, next_text))
                index += 1
            elements.append(StructuredElement(
                element_id=f"ocr-list-{page_result.page_index}-{len(elements)}",
                type=StructuredElementType.LIST,
                page_index=page_result.page_index,
                text="\n".join(value[1] for value in list_values),
                bbox=_bbox_union([item.source_bbox for item in list_lines]),
                source="tesseract_ocr_structure",
                confidence=0.76,
                ordered=ordered,
                data={"items": [{"item_index": item_index, "text": value[1], "level": 0, "marker": f"{item_index + 1}." if ordered else "-"} for item_index, value in enumerate(list_values)]},
            ))
            index += 1
            continue
        if current and (_line_gap(current[-1], line) > median_height * 1.9 or not compatible(current[-1], line)):
            flush_paragraph()
        current.append(line)
        index += 1
    flush_paragraph()
    if table is not None:
        elements.append(table)
    return sorted(elements, key=_element_sort_key)


def _table_elements(pdf_page: Any, page_index: int) -> list[StructuredElement]:
    elements: list[StructuredElement] = []
    for index, table in enumerate(extract_table_nodes_from_pdfplumber(pdf_page, page_index + 1)):
        rows = [[{"text": cell.text, "rowspan": cell.rowspan, "colspan": cell.colspan} for cell in row] for row in table.rows]
        headers = [{"text": cell.text, "rowspan": cell.rowspan, "colspan": cell.colspan} for cell in table.headers]
        elements.append(StructuredElement(
            element_id=f"native-table-{page_index}-{index}",
            type=StructuredElementType.TABLE,
            page_index=page_index,
            text="\n".join(" | ".join(cell["text"] for cell in row) for row in ([headers] + rows if headers else rows)),
            bbox={"x": table.bbox.x0, "y": table.bbox.y0, "width": table.bbox.x1 - table.bbox.x0, "height": table.bbox.y1 - table.bbox.y0},
            source="pdfplumber_native",
            data={"headers": headers, "rows": rows, "row_count": len(rows), "column_count": len(headers) if headers else max((len(row) for row in rows), default=0)},
        ))
    return elements


def _validate_result(pages: tuple[StructuredPage, ...]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_ids = {element.element_id for element in page.elements}
        if len(page_ids) != len(page.elements):
            issues.append({"code": "DUPLICATE_ELEMENT_ID", "page_index": page.page_index})
        if set(page.reading_order) != page_ids:
            issues.append({"code": "READING_ORDER_MISMATCH", "page_index": page.page_index})
        if seen.intersection(page_ids):
            issues.append({"code": "DUPLICATE_ELEMENT_ID", "page_index": page.page_index})
        seen.update(page_ids)
        for element in page.elements:
            if element.bbox:
                if element.bbox["x"] < 0 or element.bbox["y"] < 0 or element.bbox["x"] + element.bbox["width"] > page.geometry["width"] + 1e-3 or element.bbox["y"] + element.bbox["height"] > page.geometry["height"] + 1e-3:
                    issues.append({"code": "ELEMENT_OUT_OF_BOUNDS", "page_index": page.page_index, "element_id": element.element_id})
    return {"valid": not issues, "issues": issues}


def render_structured_markdown(result: StructuredDocumentResult, *, emit_page_breaks: bool = True) -> str:
    """Render only canonical structured elements into GFM Markdown."""
    chunks: list[str] = []
    for page_index, page in enumerate(result.pages):
        if emit_page_breaks and page_index:
            chunks.append("\n<!-- pagebreak -->\n")
        elements = {element.element_id: element for element in page.elements}
        for element_id in page.reading_order:
            element = elements[element_id]
            if element.type is StructuredElementType.HEADING:
                chunks.append(f"{'#' * max(1, min(6, element.level or 1))} {element.text}")
            elif element.type in (StructuredElementType.PARAGRAPH, StructuredElementType.TEXT_BLOCK):
                if element.text:
                    chunks.append(element.text)
            elif element.type is StructuredElementType.LIST:
                for index, item in enumerate(element.data.get("items", [])):
                    marker = f"{index + 1}." if element.ordered else "-"
                    chunks.append(f"{marker} {item.get('text', '')}")
            elif element.type is StructuredElementType.TABLE:
                headers = [str(cell.get("text", "")).replace("|", "\\|") for cell in element.data.get("headers", [])]
                rows = [[str(cell.get("text", "")).replace("|", "\\|") for cell in row] for row in element.data.get("rows", [])]
                all_rows = ([headers] if headers else []) + rows
                if all_rows:
                    columns = max(len(row) for row in all_rows)
                    all_rows = [row + [""] * (columns - len(row)) for row in all_rows]
                    chunks.append("| " + " | ".join(all_rows[0]) + " |")
                    chunks.append("| " + " | ".join(":---" for _ in range(columns)) + " |")
                    chunks.extend("| " + " | ".join(row) + " |" for row in all_rows[1:])
            elif element.type is StructuredElementType.IMAGE:
                chunks.append(f"![{element.data.get('alt_text', 'Figure')}]({element.data.get('url', '')})")
            elif element.type is StructuredElementType.CAPTION:
                chunks.append(f"*{element.text}*")
            elif element.type is StructuredElementType.FORMULA:
                if element.text:
                    chunks.append(f"$${element.text}$$")
    return "\n\n".join(chunk for chunk in chunks if chunk).strip() + "\n"


class StructuredDocumentProcessor:
    """Native-first structured document processor using current local engines."""

    def __init__(self, structured_adapter: StructuredEngineAdapter | None = None) -> None:
        self.structured_adapter = structured_adapter

    def process_document(
        self,
        pdf_path: str | Path,
        *,
        language: str = "eng",
        language_mode: str | None = None,
        languages: Sequence[str] | None = None,
        language_usage: Mapping[str, float] | None = None,
        routing_policy: str = "AUTO",
        cancellation_check: Callable[[], None] | None = None,
        page_progress_callback: Callable[[int, int, StructuredPage], None] | None = None,
    ) -> StructuredDocumentResult:
        source_path = Path(pdf_path)
        started = time.monotonic()
        source_size = source_path.stat().st_size
        if source_size > structured_max_input_bytes():
            raise ValueError("structured OCR input exceeds the configured byte limit")
        with fitz.open(str(source_path)) as source_document:
            if len(source_document) == 0 or len(source_document) > structured_max_pages():
                raise ValueError("structured OCR input exceeds the configured page limit")
        ocr_worker = OCRV2Worker(max_raster_pixels=structured_max_raster_pixels())
        from .routing import RoutePolicy
        from .validation import OCRProfile

        if routing_policy in {"FAST", "LANGUAGE_FALLBACK"}:
            ocr_worker.router.policy = RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2")
        ocr_result = ocr_worker.process_document(
            source_path,
            language=language,
            language_mode=language_mode,
            languages=languages,
            language_usage=language_usage,
            profile=OCRProfile.OCR_TEXT_V2,
            cancellation_check=cancellation_check,
            page_timeout_seconds=structured_page_timeout_seconds(),
            page_progress_callback=None,
        )
        with fitz.open(str(source_path)) as document:
            base_font_size = calculate_document_base_font_size(document)
            plumber_doc = None
            try:
                try:
                    import pdfplumber
                    plumber_doc = pdfplumber.open(str(source_path))
                except Exception:
                    plumber_doc = None
                pages: list[StructuredPage] = []
                all_capabilities: set[str] = {"STRUCTURED_DOCUMENT", "REGIONS"}
                warnings: list[str] = []
                for page_index, (pdf_page, ocr_page) in enumerate(zip(document, ocr_result.pages)):
                    if cancellation_check:
                        cancellation_check()
                    classification = ocr_page.content_classification.value
                    native = _native_elements(pdf_page, page_index, base_font_size)
                    tables: list[StructuredElement] = []
                    if plumber_doc is not None and classification in {PageContentClassification.TEXT_NATIVE.value, PageContentClassification.MIXED.value}:
                        tables = _table_elements(plumber_doc.pages[page_index], page_index)
                        table_boxes = [table.bbox for table in tables]
                        native = [element for element in native if not any(_overlaps(element.bbox, box) for box in table_boxes)]
                    ocr = _ocr_structured_elements(ocr_page)
                    if classification == PageContentClassification.TEXT_NATIVE.value or ocr_page.processing_source is PageProcessingSource.NATIVE_EXTRACTION:
                        elements = native + tables
                        processing_source = "NATIVE_EXTRACTION"
                    elif classification == PageContentClassification.MIXED.value:
                        # Pages carrying an OCR text layer are classified as
                        # MIXED, but their text layer is not necessarily
                        # trustworthy native authoring text.  Prefer the
                        # rendered OCR line structure here; otherwise the
                        # same page is duplicated and every OCR line can be
                        # mistaken for a large-font native heading.
                        elements = ocr + tables
                        processing_source = "HYBRID"
                        warnings.append(f"MIXED_PAGE_RECONCILED:{page_index}")
                    else:
                        elements = ocr + tables
                        processing_source = "OCR_RECOGNITION"
                    elements.sort(key=_element_sort_key)
                    page_caps: set[str] = {"STRUCTURED_DOCUMENT"}
                    if any(element.text for element in elements):
                        page_caps.add(ResultCapability.TEXT.value)
                    if any(element.bbox for element in elements):
                        page_caps.update({"REGIONS", ResultCapability.BLOCK_GEOMETRY.value})
                    if ocr_page.lines:
                        page_caps.add(ResultCapability.LINE_GEOMETRY.value)
                    if ocr_page.tokens:
                        page_caps.add(ResultCapability.WORD_GEOMETRY.value)
                    table_elements = [element for element in elements if element.type is StructuredElementType.TABLE]
                    if table_elements:
                        page_caps.add("TABLES")
                    if any(element.type is StructuredElementType.HEADING for element in elements):
                        page_caps.add("HEADINGS")
                    if any(element.type is StructuredElementType.LIST for element in elements):
                        page_caps.add("LISTS")
                    if classification in {PageContentClassification.IMAGE_SCAN.value, PageContentClassification.MIXED.value} and not table_elements:
                        warnings.append(f"TABLE_STRUCTURE_UNAVAILABLE:{page_index}")
                    page_warnings = tuple(warning for warning in warnings if warning.endswith(f":{page_index}"))
                    reading_order = tuple(element.element_id for element in elements)
                    geometry = {"width": ocr_page.geometry.width, "height": ocr_page.geometry.height, "rotation": ocr_page.geometry.rotation, "coordinate_space": ocr_page.geometry.coordinate_space}
                    structured_page = StructuredPage(page_index, ocr_page.page_id, geometry, classification, processing_source, "BLANK" if not elements else "SUCCESS", tuple(elements), reading_order, tuple(sorted(page_caps)), page_warnings, {
                        "requested": list(ocr_page.language.requested_languages),
                        "detected": list(ocr_page.language.detected_languages),
                        "status": ocr_page.language.language_status,
                        "mode": ocr_page.language.requested_mode,
                        "confidence": ocr_page.language.detection_confidence,
                        "scripts": list(ocr_page.language.detected_scripts),
                        "reason": ocr_page.language.detection_reason,
                    })
                    pages.append(structured_page)
                    all_capabilities.update(page_caps)
                    if page_progress_callback:
                        page_progress_callback(page_index + 1, len(document), structured_page)
            finally:
                if plumber_doc is not None:
                    plumber_doc.close()
        structured_pages = tuple(pages)
        validation = _validate_result(structured_pages)
        if "TABLES" not in all_capabilities and any(page.classification in {"IMAGE_SCAN", "MIXED"} for page in structured_pages):
            warnings.append("STRUCTURED_TABLES_REQUIRE_NATIVE_TABLES_OR_STRUCTURED_ENGINE")
        if any(page.classification in {"IMAGE_SCAN", "MIXED"} for page in structured_pages):
            warnings.append("FORMULA_STRUCTURE_UNAVAILABLE_WITH_CURRENT_LOCAL_ENGINES")
        available = {"STRUCTURED_DOCUMENT", "REGIONS", "BLOCK_GEOMETRY", "LINE_GEOMETRY", "WORD_GEOMETRY", "READING_ORDER", "HEADINGS", "LISTS", "TABLES", "FORMULAS", "IMAGE", "CAPTION"}
        document_elements = [StructuredElement(
            element_id="document-0",
            type=StructuredElementType.DOCUMENT,
            page_index=-1,
            source="ocr_v2_structured_normalizer",
            data={"page_ids": [page.page_id for page in structured_pages]},
        )]
        document_elements.extend(
            StructuredElement(
                element_id=f"page-{page.page_index}",
                type=StructuredElementType.PAGE,
                page_index=page.page_index,
                bbox={"x": 0.0, "y": 0.0, "width": page.geometry["width"], "height": page.geometry["height"]},
                source="ocr_v2_structured_normalizer",
                data={"page_id": page.page_id, "element_ids": list(page.reading_order)},
            )
            for page in structured_pages
        )
        return StructuredDocumentResult(
            schema_version=STRUCTURED_SCHEMA_VERSION,
            result_id=str(uuid.uuid4()),
            # Result JSON is a product boundary; never expose the worker's
            # local input path.  The generated result id correlates the
            # durable artifact without disclosing storage or filesystem data.
            source={"source_id": "structured-document", "filename": source_path.name, "page_count": len(structured_pages)},
            elements=tuple(document_elements),
            pages=structured_pages,
            capabilities=tuple(sorted(all_capabilities)),
            available_capabilities=tuple(sorted(available)),
            warnings=tuple(dict.fromkeys(warnings)),
            validation=validation | {"elapsed_seconds": time.monotonic() - started},
        )

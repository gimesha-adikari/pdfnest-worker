"""Canonical structured-document extraction for OCR V2.

This module is deliberately engine-neutral.  Native PDF blocks and tables are
kept native; scanned pages are represented from the existing Tesseract OCR V2
result.  It does not infer tables, formulas, headings, or captions from plain
OCR text when the source does not provide that structure.
"""

from __future__ import annotations

import time
import uuid
import os
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
    bbox = element.bbox or {}
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
                    ocr = _ocr_elements(ocr_page)
                    if classification == PageContentClassification.TEXT_NATIVE.value or ocr_page.processing_source is PageProcessingSource.NATIVE_EXTRACTION:
                        elements = native + tables
                        processing_source = "NATIVE_EXTRACTION"
                    elif classification == PageContentClassification.MIXED.value:
                        surviving_ocr = [element for element in ocr if not any(_overlaps(element.bbox, native_element.bbox) for native_element in native)]
                        elements = native + tables + surviving_ocr
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
                    if tables:
                        page_caps.add("TABLES")
                    if any(element.type is StructuredElementType.HEADING for element in elements):
                        page_caps.add("HEADINGS")
                    if any(element.type is StructuredElementType.LIST for element in elements):
                        page_caps.add("LISTS")
                    if classification in {PageContentClassification.IMAGE_SCAN.value, PageContentClassification.MIXED.value} and not tables:
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

"""Shared OCR-aware markup selection and annotation foundation.

This module is the only V2 path that resolves text selections for Highlight,
Underline, and Strikeout.  It consumes the canonical OCR V2 result; it never
calls an OCR engine directly.  Legacy markup remains in ``app.api.tools``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz

from .contracts import DocumentResult, OCRToken, PageProcessingSource, Rect
from .errors import (
    AnnotationWriteError,
    EngineUnavailableError,
    OCRTimeoutError,
    WordGeometryUnavailableError,
    TextNotFoundError,
)
from .orchestration import OCRV2Worker
from .routing import RoutePolicy
from .validation import OCRProfile


class MarkupAction(str, Enum):
    HIGHLIGHT = "highlight"
    UNDERLINE = "underline"
    STRIKEOUT = "strikeout"


class MarkupMode(str, Enum):
    SMART = "smart"
    OCR = "ocr"
    NATIVE = "native"


class MarkupSourceType(str, Enum):
    NATIVE = "native"
    OCR = "ocr"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class CanonicalMarkupWord:
    id: str
    text: str
    page_index: int
    bbox: Rect
    confidence: float | None
    source_type: MarkupSourceType
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class MarkupSelection:
    page_index: int
    matched_text: str
    word_ids: tuple[str, ...]
    reading_order_start: int
    reading_order_end: int
    words: tuple[CanonicalMarkupWord, ...]
    group_rects: tuple[Rect, ...]
    source_type: MarkupSourceType
    confidence: tuple[float, ...]
    provenance: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, Rect):
                return asdict(value)
            if isinstance(value, CanonicalMarkupWord):
                return {key: convert(item) for key, item in asdict(value).items()}
            return value

        return {key: convert(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class MarkupExecutionResult:
    action: MarkupAction
    mode: MarkupMode
    source_policy: str
    page_count: int
    selections: tuple[MarkupSelection, ...]
    page_sources: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ocr_v2_markup_result.v1",
            "action": self.action.value,
            "mode": self.mode.value,
            "source_policy": self.source_policy,
            "page_count": self.page_count,
            "selection_count": len(self.selections),
            "selections": [selection.to_dict() for selection in self.selections],
            "page_sources": list(self.page_sources),
        }


def _query_tokens(value: str) -> tuple[str, ...]:
    # Exact matching is performed over canonical reading-order tokens.  Word
    # punctuation is retained as a token so punctuation-sensitive queries do
    # not silently become fuzzy matches.
    return tuple(token.casefold() for token in re.findall(r"\w+|[^\w\s]", value, flags=re.UNICODE))


def _token_parts(value: str) -> tuple[str, ...]:
    return _query_tokens(value)


def _source_type(page: Any) -> MarkupSourceType:
    source = page.processing_source
    if source is PageProcessingSource.NATIVE_EXTRACTION:
        return MarkupSourceType.NATIVE
    if source is PageProcessingSource.HYBRID:
        return MarkupSourceType.HYBRID
    return MarkupSourceType.OCR


def _canonical_words(page: Any, mode: MarkupMode) -> tuple[CanonicalMarkupWord, ...]:
    source_type = _source_type(page)
    if mode is MarkupMode.NATIVE and source_type is not MarkupSourceType.NATIVE:
        return ()
    if mode is MarkupMode.OCR and source_type is MarkupSourceType.NATIVE:
        return ()
    tokens_by_id = page.tokens_by_id
    words: list[CanonicalMarkupWord] = []
    for token_id in page.reading_order:
        token = tokens_by_id.get(token_id)
        if token is None or not token.text.strip():
            continue
        words.append(
            CanonicalMarkupWord(
                id=token.id,
                text=token.text,
                page_index=page.page_index,
                bbox=token.bbox,
                confidence=token.confidence.raw_value if token.confidence else None,
                source_type=source_type,
                provenance=tuple(page.provenance_refs),
            )
        )
    return tuple(words)


def _line_groups(words: tuple[CanonicalMarkupWord, ...]) -> tuple[Rect, ...]:
    groups: list[list[CanonicalMarkupWord]] = []
    for word in words:
        if not groups:
            groups.append([word])
            continue
        current = groups[-1]
        current_rect = Rect(
            min(item.bbox.x for item in current),
            min(item.bbox.y for item in current),
            max(item.bbox.x1 for item in current) - min(item.bbox.x for item in current),
            max(item.bbox.y1 for item in current) - min(item.bbox.y for item in current),
        )
        if abs(word.bbox.y - current_rect.y) <= max(current_rect.height, word.bbox.height) * 0.6:
            current.append(word)
        else:
            groups.append([word])
    return tuple(
        Rect(
            min(item.bbox.x for item in group),
            min(item.bbox.y for item in group),
            max(item.bbox.x1 for item in group) - min(item.bbox.x for item in group),
            max(item.bbox.y1 for item in group) - min(item.bbox.y for item in group),
        )
        for group in groups
    )


def select_query(result: DocumentResult, query: str, *, mode: MarkupMode = MarkupMode.SMART) -> tuple[MarkupSelection, ...]:
    wanted = _query_tokens(query)
    if not wanted:
        raise TextNotFoundError("text query is empty")

    selections: list[MarkupSelection] = []
    for page in result.pages:
        words = _canonical_words(page, mode)
        if not words:
            continue
        flattened: list[str] = []
        word_indices: list[int] = []
        for index, word in enumerate(words):
            parts = _token_parts(word.text)
            flattened.extend(parts)
            word_indices.extend([index] * len(parts))
        for start in range(0, len(flattened) - len(wanted) + 1):
            if tuple(flattened[start : start + len(wanted)]) != wanted:
                continue
            selected_indexes = tuple(dict.fromkeys(word_indices[start : start + len(wanted)]))
            selected_words = tuple(words[index] for index in selected_indexes)
            confidence = tuple(word.confidence for word in selected_words if word.confidence is not None)
            selections.append(
                MarkupSelection(
                    page_index=page.page_index,
                    matched_text=" ".join(word.text for word in selected_words),
                    word_ids=tuple(word.id for word in selected_words),
                    reading_order_start=start,
                    reading_order_end=start + len(wanted) - 1,
                    words=selected_words,
                    group_rects=_line_groups(selected_words),
                    source_type=selected_words[0].source_type,
                    confidence=confidence,
                    provenance=tuple(sorted({ref for word in selected_words for ref in word.provenance})),
                )
            )
    if not selections:
        if any(page.text.strip() for page in result.pages) and not any(page.tokens for page in result.pages):
            raise WordGeometryUnavailableError("canonical OCR result has no genuine word geometry")
        raise TextNotFoundError(f"text query was not found: {query[:80]}")
    return tuple(selections)


def select_regions(
    result: DocumentResult,
    boxes: list[dict[str, Any]],
    *,
    mode: MarkupMode = MarkupMode.SMART,
) -> tuple[MarkupSelection, ...]:
    """Resolve visible page rectangles against canonical word geometry."""
    selections: list[MarkupSelection] = []
    for box in boxes:
        try:
            page_index = int(box.get("page", 0)) - 1
            x = float(box.get("x", 0))
            y = float(box.get("y", 0))
            width = float(box.get("width", 0))
            height = float(box.get("height", 0))
        except (TypeError, ValueError):
            continue
        if page_index < 0 or page_index >= len(result.pages) or width <= 0 or height <= 0:
            continue
        page = result.pages[page_index]
        words = _canonical_words(page, mode)
        if not words:
            if page.text.strip() and "WORD_GEOMETRY" not in page.capabilities:
                raise WordGeometryUnavailableError("canonical OCR result has no genuine word geometry")
            continue
        region = Rect(x, y, width, height)
        selected = tuple(
            (index, word)
            for index, word in enumerate(words)
            if word.bbox.x < region.x1 and word.bbox.x1 > region.x and word.bbox.y < region.y1 and word.bbox.y1 > region.y
        )
        if not selected:
            continue
        selected_words = tuple(word for _, word in selected)
        selections.append(
            MarkupSelection(
                page_index=page_index,
                matched_text=" ".join(word.text for word in selected_words),
                word_ids=tuple(word.id for word in selected_words),
                reading_order_start=selected[0][0],
                reading_order_end=selected[-1][0],
                words=selected_words,
                group_rects=_line_groups(selected_words),
                source_type=selected_words[0].source_type,
                confidence=tuple(word.confidence for word in selected_words if word.confidence is not None),
                provenance=tuple(sorted({ref for word in selected_words for ref in word.provenance})),
            )
        )
    if not selections and any(page.text.strip() for page in result.pages):
        raise TextNotFoundError("no canonical words intersected the selected regions")
    return tuple(selections)


def _annotate(page: fitz.Page, selection: MarkupSelection, action: MarkupAction, color: tuple[float, float, float]) -> None:
    try:
        # PyMuPDF accepts rectangles here and converts them to axis-aligned
        # annotation quads while retaining the canonical PDF-point geometry.
        quads = [fitz.Rect(rect.x, rect.y, rect.x1, rect.y1) for rect in selection.group_rects]
        if action is MarkupAction.HIGHLIGHT:
            annotation = page.add_highlight_annot(quads)
        elif action is MarkupAction.UNDERLINE:
            annotation = page.add_underline_annot(quads)
        else:
            annotation = page.add_strikeout_annot(quads)
        if annotation is None:
            raise RuntimeError("PyMuPDF did not create the annotation")
        annotation.set_colors(stroke=color)
        annotation.update()
    except Exception as exc:
        raise AnnotationWriteError(f"could not write {action.value} annotation") from exc


def apply_ocr_markup(
    input_path: str | Path,
    output_path: str | Path,
    *,
    action: MarkupAction,
    query: str,
    language: str = "eng",
    mode: MarkupMode = MarkupMode.SMART,
    color: tuple[float, float, float] = (1.0, 1.0, 0.0),
    cancellation_check: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> MarkupExecutionResult:
    """Run canonical OCR once, resolve all matches, then annotate the PDF."""
    worker = OCRV2Worker(route_policy=RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2"))
    result = worker.process_document(
        input_path,
        language=language,
        profile=OCRProfile.OCR_TEXT_V2,
        cancellation_check=cancellation_check,
        page_progress_callback=lambda done, total, _page: progress_callback(done, total) if progress_callback else None,
    )
    failed = next((page for page in result.pages if page.status.value == "FAILED"), None)
    if failed:
        if failed.failure_code == "EngineUnavailableError":
            raise EngineUnavailableError("OCR engine was unavailable while processing a markup page")
        if failed.failure_code == "OCRTimeoutError":
            raise OCRTimeoutError("OCR exceeded the markup page deadline")
        raise WordGeometryUnavailableError(f"OCR page {failed.page_index} did not produce selectable word geometry")
    selections = select_query(result, query, mode=mode)
    with fitz.open(str(input_path)) as document:
        for selection in selections:
            _annotate(document[selection.page_index], selection, action, color)
        document.save(str(output_path), garbage=4, deflate=True)
    page_sources = tuple(
        {
            "page_index": page.page_index,
            "source_type": _source_type(page).value,
            "classification": page.content_classification.value,
            "word_geometry": "WORD_GEOMETRY" in page.capabilities,
            "reading_order": "READING_ORDER" in page.capabilities,
        }
        for page in result.pages
    )
    return MarkupExecutionResult(
        action=action,
        mode=mode,
        source_policy="NATIVE_TRUSTED_ELSE_OCR_V2; MIXED_USES_SINGLE_OCR_SOURCE_TO_AVOID_DUPLICATES",
        page_count=len(result.pages),
        selections=selections,
        page_sources=page_sources,
    )

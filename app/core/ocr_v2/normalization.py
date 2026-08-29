"""Mapping from engine-native output to canonical V2 page results."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .contracts import (
    Confidence,
    LanguageMetadata,
    OCRBlock,
    OCRLine,
    OCRToken,
    PageContentClassification,
    PageGeometry,
    PageProcessingSource,
    PageResult,
    PageStatus,
    Rect,
    ResultCapability,
    UnnormalizedPageOutput,
    Validation,
)
from .geometry import pixel_rect_to_points


def _rect(item: dict[str, Any], output: UnnormalizedPageOutput, geometry: PageGeometry) -> Rect:
    box = item.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("engine item has no four-value bbox")
    values = tuple(float(value) for value in box)
    if output.coordinate_space == "pixel_top_left":
        return pixel_rect_to_points(values, geometry)
    if output.coordinate_space == "pdf_points_visible_cropbox_top_left":
        x0, y0, x1, y1 = values
        return Rect(x0, y0, x1 - x0, y1 - y0)
    raise ValueError(f"unsupported engine coordinate space: {output.coordinate_space}")


def normalize_page_output(
    output: UnnormalizedPageOutput,
    *,
    page_index: int,
    geometry: PageGeometry,
    classification: PageContentClassification,
    processing_source: PageProcessingSource,
    language: LanguageMetadata | None = None,
) -> PageResult:
    token_items = [dict(item) for item in output.items if item.get("kind") != "line" and item.get("text")]
    line_items = [dict(item) for item in output.items if item.get("kind") == "line"]
    tokens: list[OCRToken] = []
    for item in token_items:
        confidence = item.get("confidence")
        scale = str(item.get("confidence_scale", "0_1"))
        token_confidence = Confidence(float(confidence), scale, output.provenance.producer_id if output.provenance else "engine") if confidence is not None else None
        tokens.append(OCRToken(id=str(item.get("id", f"page-{page_index}-token-{len(tokens)}")), text=str(item["text"]), bbox=_rect(item, output, geometry), confidence=token_confidence, line_id=item.get("line_id"), block_id=item.get("block_id"), provenance_ref=output.provenance.producer_id if output.provenance else None))
    by_token = {token.id: token for token in tokens}
    lines: list[OCRLine] = []
    if line_items:
        for item in line_items:
            lines.append(OCRLine(id=str(item["id"]), text=str(item.get("text", "")), bbox=_rect(item, output, geometry), token_ids=tuple(str(value) for value in item.get("token_ids", ()))) )
    else:
        grouped: dict[str, list[OCRToken]] = defaultdict(list)
        for token in tokens:
            grouped[token.line_id or f"line-{len(grouped)}"].append(token)
        for line_id, line_tokens in grouped.items():
            lines.append(OCRLine(id=str(line_id), text=" ".join(token.text for token in line_tokens), bbox=Rect(min(token.bbox.x for token in line_tokens), min(token.bbox.y for token in line_tokens), max(token.bbox.x1 for token in line_tokens) - min(token.bbox.x for token in line_tokens), max(token.bbox.y1 for token in line_tokens) - min(token.bbox.y for token in line_tokens)), token_ids=tuple(token.id for token in line_tokens)))
    blocks: list[OCRBlock] = []
    grouped_lines: dict[str, list[OCRLine]] = defaultdict(list)
    for line in lines:
        block_id = next((token.block_id for token in tokens if token.id in line.token_ids and token.block_id), line.id)
        grouped_lines[str(block_id)].append(line)
    for block_id, block_lines in grouped_lines.items():
        blocks.append(OCRBlock(id=block_id, text="\n".join(line.text for line in block_lines), bbox=Rect(min(line.bbox.x for line in block_lines), min(line.bbox.y for line in block_lines), max(line.bbox.x1 for line in block_lines) - min(line.bbox.x for line in block_lines), max(line.bbox.y1 for line in block_lines) - min(line.bbox.y for line in block_lines)), line_ids=tuple(line.id for line in block_lines)))
    reading_order = tuple(token.id for token in tokens)
    capabilities = {ResultCapability.TEXT.value} if output.text is not None else set()
    if tokens:
        capabilities.update({ResultCapability.WORD_GEOMETRY.value, ResultCapability.READING_ORDER.value})
    if lines:
        capabilities.add(ResultCapability.LINE_GEOMETRY.value)
    if blocks:
        capabilities.add(ResultCapability.BLOCK_GEOMETRY.value)
    if any(token.confidence is not None for token in tokens):
        capabilities.add(ResultCapability.CONFIDENCE.value)
    if language and language.requested_languages:
        capabilities.add(ResultCapability.LANGUAGE_METADATA.value)
    if classification in (PageContentClassification.BLANK, PageContentClassification.NEAR_BLANK) and not output.text.strip():
        status = PageStatus.BLANK
    elif output.text.strip():
        status = PageStatus.SUCCESS
    else:
        status = PageStatus.PARTIAL
    return PageResult(page_index=page_index, page_id=output.page_id, geometry=geometry, content_classification=classification, processing_source=processing_source, status=status, text=output.text, tokens=tuple(tokens), lines=tuple(lines), blocks=tuple(blocks), reading_order=reading_order, language=language or LanguageMetadata(), capabilities=frozenset(capabilities), provenance_refs=(output.provenance.producer_id,) if output.provenance else (), validation=Validation(True))

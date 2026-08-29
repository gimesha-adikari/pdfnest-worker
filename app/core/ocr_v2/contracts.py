"""Canonical, engine-neutral OCR V2 contracts.

The package is deliberately separate from ``app.api.tools.ocr``.  V1 keeps
its existing endpoint and result behavior; these contracts are the boundary
for the next worker-core implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class ResultCapability(str, Enum):
    TEXT = "TEXT"
    WORD_GEOMETRY = "WORD_GEOMETRY"
    LINE_GEOMETRY = "LINE_GEOMETRY"
    BLOCK_GEOMETRY = "BLOCK_GEOMETRY"
    CONFIDENCE = "CONFIDENCE"
    LANGUAGE_METADATA = "LANGUAGE_METADATA"
    SCRIPT_METADATA = "SCRIPT_METADATA"
    READING_ORDER = "READING_ORDER"
    POLYGON_GEOMETRY = "POLYGON_GEOMETRY"


class StructuredCapability(str, Enum):
    STRUCTURED_DOCUMENT = "STRUCTURED_DOCUMENT"
    REGIONS = "REGIONS"
    TABLES = "TABLES"
    FORMULAS = "FORMULAS"
    HEADINGS = "HEADINGS"
    LISTS = "LISTS"


class ExecutionCapability(str, Enum):
    CANCELLATION = "CANCELLATION"
    BATCHING = "BATCHING"
    PAGE_INDEPENDENT = "PAGE_INDEPENDENT"


class PageContentClassification(str, Enum):
    TEXT_NATIVE = "TEXT_NATIVE"
    IMAGE_SCAN = "IMAGE_SCAN"
    MIXED = "MIXED"
    BLANK = "BLANK"
    NEAR_BLANK = "NEAR_BLANK"
    SUSPICIOUS_TEXT_LAYER = "SUSPICIOUS_TEXT_LAYER"
    UNKNOWN = "UNKNOWN"


class PageProcessingSource(str, Enum):
    NONE = "NONE"
    NATIVE_EXTRACTION = "NATIVE_EXTRACTION"
    OCR_RECOGNITION = "OCR_RECOGNITION"
    HYBRID = "HYBRID"


class PageStatus(str, Enum):
    SUCCESS = "SUCCESS"
    BLANK = "BLANK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Rect:
    """A continuous x/y/width/height rectangle in PDF points."""

    x: float
    y: float
    width: float
    height: float

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class PageGeometry:
    width: float
    height: float
    rotation: int = 0
    coordinate_space: str = "pdf_points_visible_cropbox_top_left"
    pixel_width: int | None = None
    pixel_height: int | None = None


@dataclass(frozen=True)
class Confidence:
    raw_value: float
    scale: str
    source: str
    calibrated: bool = False


@dataclass(frozen=True)
class LanguageMetadata:
    requested_languages: tuple[str, ...] = ()
    detected_languages: tuple[str, ...] = ()
    language_status: str = "NOT_DETECTED"
    detected_scripts: tuple[str, ...] = ()
    script_status: str = "NOT_DETECTED"


@dataclass(frozen=True)
class OCRToken:
    id: str
    text: str
    bbox: Rect
    confidence: Confidence | None = None
    language: str | None = None
    script: str | None = None
    line_id: str | None = None
    block_id: str | None = None
    provenance_ref: str | None = None


@dataclass(frozen=True)
class OCRLine:
    id: str
    text: str
    bbox: Rect
    token_ids: tuple[str, ...] = ()
    provenance_ref: str | None = None


@dataclass(frozen=True)
class OCRBlock:
    id: str
    text: str
    bbox: Rect
    line_ids: tuple[str, ...] = ()
    provenance_ref: str | None = None


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "ERROR"
    path: str | None = None


@dataclass(frozen=True)
class Validation:
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class Provenance:
    producer_id: str
    producer_version: str | None = None
    model_identity: str | None = None
    source: str | None = None
    configuration: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnnormalizedPageOutput:
    """Engine output before it is mapped into canonical PDF-point geometry."""

    page_id: str
    text: str
    items: tuple[Mapping[str, Any], ...] = ()
    coordinate_space: str = "unknown"
    provenance: Provenance | None = None
    raw_output: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageResult:
    page_index: int
    page_id: str
    geometry: PageGeometry
    content_classification: PageContentClassification
    processing_source: PageProcessingSource
    status: PageStatus
    text: str
    tokens: tuple[OCRToken, ...] = ()
    lines: tuple[OCRLine, ...] = ()
    blocks: tuple[OCRBlock, ...] = ()
    reading_order: tuple[str, ...] = ()
    language: LanguageMetadata = field(default_factory=LanguageMetadata)
    capabilities: frozenset[str] = frozenset()
    provenance_refs: tuple[str, ...] = ()
    validation: Validation = field(default_factory=lambda: Validation(True))
    failure_code: str | None = None
    failure_message: str | None = None

    @property
    def tokens_by_id(self) -> dict[str, OCRToken]:
        return {token.id: token for token in self.tokens}


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    page_count: int
    filename: str | None = None


@dataclass(frozen=True)
class DocumentResult:
    schema_version: str
    result_id: str
    source: SourceMetadata
    pages: tuple[PageResult, ...]
    capabilities: frozenset[str] = frozenset()
    provenance: tuple[Provenance, ...] = ()
    validation: Validation = field(default_factory=lambda: Validation(True))

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, frozenset | set | tuple):
                return [convert(item) for item in value]
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in value.items()}
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            return value

        return convert(self)


def capability_names(values: set[Enum] | frozenset[Enum] | tuple[Enum, ...]) -> frozenset[str]:
    return frozenset(value.value for value in values)

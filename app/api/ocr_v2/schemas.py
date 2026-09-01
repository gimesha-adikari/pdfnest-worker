"""Versioned worker-side OCR Text V2 request and safe response schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.ocr_v2.language_policy import OCRLanguageMode, OCRLanguagePolicy


class OCRV2Profile(str, Enum):
    OCR_TEXT_V2 = "OCR_TEXT_V2"
    SEARCHABLE_PDF_V2 = "SEARCHABLE_PDF_V2"
    DOCUMENT_EXTRACTION_V2 = "DOCUMENT_EXTRACTION_V2"
    PDF_MARKDOWN_V2 = "PDF_MARKDOWN_V2"
    MARKUP_V2 = "MARKUP_V2"


class OCRV2RoutingPolicy(str, Enum):
    AUTO = "AUTO"
    FAST = "FAST"
    QUALITY = "QUALITY"
    GEOMETRY = "GEOMETRY"
    LANGUAGE_FALLBACK = "LANGUAGE_FALLBACK"


class OCRV2WorkerRequest(BaseModel):
    schema_version: str = Field(default="ocr_v2_worker_request.v1", pattern=r"^ocr_v2_worker_request\.v1$")
    request_id: str = Field(min_length=1, max_length=128)
    profile: OCRV2Profile = OCRV2Profile.OCR_TEXT_V2
    language: str = Field(default="eng", max_length=128)
    language_mode: OCRLanguageMode = OCRLanguageMode.EXPLICIT
    languages: list[str] = Field(default_factory=list, max_length=16)
    language_usage: dict[str, float] = Field(default_factory=dict, max_length=32)
    routing_policy: OCRV2RoutingPolicy = OCRV2RoutingPolicy.AUTO

    @model_validator(mode="after")
    def normalize_language_policy(self) -> "OCRV2WorkerRequest":
        policy = OCRLanguagePolicy.from_request(
            self.language,
            mode=self.language_mode,
            languages=self.languages or None,
        )
        self.language = "auto" if policy.mode is OCRLanguageMode.AUTO else policy.engine_expression
        self.languages = list(policy.languages)
        return self


class OCRV2PageResponse(BaseModel):
    page_index: int
    page_id: str
    status: str
    text: str
    classification: str
    source: str
    language: dict[str, Any] = Field(default_factory=dict)
    warning_codes: list[str] = Field(default_factory=list)


class OCRV2ErrorResponse(BaseModel):
    code: str
    message: str


class OCRV2WorkerResponse(BaseModel):
    schema_version: str = "ocr_v2_worker_response.v1"
    request_id: str
    profile: str
    status: str
    text: str
    pages: list[OCRV2PageResponse]
    warnings: list[str] = Field(default_factory=list)
    error: OCRV2ErrorResponse | None = None


class OCRV2MarkupPreviewRequest(BaseModel):
    """Request for a temporary, authenticated markup-selection projection."""

    schema_version: str = Field(default="ocr_v2_markup_preview_request.v1", pattern=r"^ocr_v2_markup_preview_request\.v1$")
    request_id: str = Field(min_length=1, max_length=128)
    profile: OCRV2Profile = OCRV2Profile.MARKUP_V2
    language: str = Field(default="eng", max_length=128)
    language_mode: OCRLanguageMode = OCRLanguageMode.EXPLICIT
    languages: list[str] = Field(default_factory=list, max_length=16)
    language_usage: dict[str, float] = Field(default_factory=dict, max_length=32)
    routing_policy: OCRV2RoutingPolicy = OCRV2RoutingPolicy.FAST

    @model_validator(mode="after")
    def normalize_language_policy(self) -> "OCRV2MarkupPreviewRequest":
        if self.profile is not OCRV2Profile.MARKUP_V2:
            raise ValueError("markup preview requires the MARKUP_V2 profile")
        policy = OCRLanguagePolicy.from_request(
            self.language,
            mode=self.language_mode,
            languages=self.languages or None,
        )
        self.language = "auto" if policy.mode is OCRLanguageMode.AUTO else policy.engine_expression
        self.languages = list(policy.languages)
        return self


class OCRV2MarkupPreviewWordResponse(BaseModel):
    id: str
    text: str
    x: float
    y: float
    width: float
    height: float
    order: int
    confidence: float | None = None


class OCRV2MarkupPreviewPageResponse(BaseModel):
    page_index: int
    page_number: int
    page_id: str
    width: float
    height: float
    rotation: int
    coordinate_space: str
    crop_box: list[float] | None = None
    classification: str
    kind: str
    selection_mode: str
    status: str
    has_selectable_text: bool
    word_count: int
    reading_order: list[str] = Field(default_factory=list)
    words: list[OCRV2MarkupPreviewWordResponse] = Field(default_factory=list)
    language: dict[str, Any] = Field(default_factory=dict)


class OCRV2MarkupPreviewResponse(BaseModel):
    schema_version: str = "ocr_v2_markup_preview.v1"
    profile: str = "MARKUP_V2"
    status: str
    page_count: int
    pages: list[OCRV2MarkupPreviewPageResponse]


class OCRV2JobSubmitRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    profile: OCRV2Profile = OCRV2Profile.OCR_TEXT_V2
    language: str = Field(default="eng", max_length=128)
    language_mode: OCRLanguageMode = OCRLanguageMode.EXPLICIT
    languages: list[str] = Field(default_factory=list, max_length=16)
    language_usage: dict[str, float] = Field(default_factory=dict, max_length=32)
    routing_policy: OCRV2RoutingPolicy = OCRV2RoutingPolicy.AUTO
    source_key: str | None = Field(default=None, max_length=512)
    source_files: list[dict[str, str]] = Field(default_factory=list, max_length=150)
    source_name: str = Field(default="document.pdf", min_length=1, max_length=255)
    owner_identity: str = Field(min_length=1, max_length=255)
    total_pages: int = Field(default=0, ge=0, le=10000)
    markup_action: str | None = Field(default=None, pattern=r"^(highlight|underline|strikeout)$")
    markup_mode: str = Field(default="smart", pattern=r"^(smart|ocr|native)$")
    markup_query: str | None = Field(default=None, max_length=500)
    markup_color: str = Field(default="#FFFF00", pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def validate_sources(self) -> "OCRV2JobSubmitRequest":
        policy = OCRLanguagePolicy.from_request(self.language, mode=self.language_mode, languages=self.languages or None)
        self.language = "auto" if policy.mode is OCRLanguageMode.AUTO else policy.engine_expression
        self.languages = list(policy.languages)
        if self.profile is not OCRV2Profile.SEARCHABLE_PDF_V2 and not self.source_key:
            raise ValueError("Document OCR profiles require source_key")
        if self.profile is OCRV2Profile.SEARCHABLE_PDF_V2 and not self.source_files:
            raise ValueError("Searchable PDF V2 requires ordered source_files")
        if self.profile is OCRV2Profile.MARKUP_V2 and (not self.markup_action or not self.markup_query or not self.markup_query.strip()):
            raise ValueError("Markup V2 requires an action and text query")
        return self


class OCRV2JobCancelRequest(BaseModel):
    owner_identity: str = Field(min_length=1, max_length=255)


class OCRV2JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: int
    total_pages: int
    completed_pages: int
    failed_pages: list[int] = Field(default_factory=list)
    current_page: int | None = None
    page_statuses: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    result_key: str | None = None
    owner_identity: str
    error_code: str | None = None
    error: str | None = None
    profile: str = "OCR_TEXT_V2"
    language: str = ""
    routing_policy: str = "AUTO"

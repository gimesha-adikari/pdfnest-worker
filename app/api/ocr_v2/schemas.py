"""Versioned worker-side OCR Text V2 request and safe response schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class OCRV2Profile(str, Enum):
    OCR_TEXT_V2 = "OCR_TEXT_V2"


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
    language: str = Field(min_length=1, max_length=128)
    routing_policy: OCRV2RoutingPolicy = OCRV2RoutingPolicy.AUTO


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


class OCRV2JobSubmitRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    profile: OCRV2Profile = OCRV2Profile.OCR_TEXT_V2
    language: str = Field(min_length=1, max_length=128)
    routing_policy: OCRV2RoutingPolicy = OCRV2RoutingPolicy.AUTO
    source_key: str = Field(min_length=1, max_length=512)
    source_name: str = Field(default="document.pdf", min_length=1, max_length=255)
    owner_identity: str = Field(min_length=1, max_length=255)
    total_pages: int = Field(default=0, ge=0, le=10000)


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

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExtractPageElement(BaseModel):
    text: str
    x: float
    y: float
    width: float
    height: float
    size: float
    font: str
    bg_color: str = Field(default="#ffffff")
    text_color: str = Field(default="#000000")


class ExtractPage(BaseModel):
    page_num: int
    width: float
    height: float
    elements: list[ExtractPageElement]
    kind: Literal["text", "mixed", "scanned", "blank"]
    has_selectable_text: bool = False
    word_count: int = 0
    text_block_count: int = 0
    image_block_count: int = 0
    is_ocr: bool | None = None


class ExtractResponse(BaseModel):
    success: bool = True
    pages: list[ExtractPage]
    source_tracker: str | None = None
    upright_tracker: str | None = None
    error: str | None = None


class CompileResponse(BaseModel):
    success: bool = True
    output_pdf_path: str | None = None
    error: str | None = None


class JobSubmissionResponse(BaseModel):
    job_id: str
    status: str = "queued"
    queue_name: str = "editor"
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MarkupMode = Literal["manual", "smart", "text", "ocr"]
MarkupAction = Literal["highlight", "underline", "strikeout"]


class MarkupBox(BaseModel):
    id: str | None = None
    x: float
    y: float
    width: float
    height: float
    page: int
    color: str = Field(default="#FFFF00")


class MarkupJobPayload(BaseModel):
    boxes: list[MarkupBox]
    mode: MarkupMode = "smart"
    file_password: str | None = None


class MarkupJobResult(BaseModel):
    artifact_path: str | None = None
    action: MarkupAction
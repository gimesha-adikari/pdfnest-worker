from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


OfficeOutputFormat = Literal["docx", "xlsx", "pptx"]


class OfficeConvertRequest(BaseModel):
    format: OfficeOutputFormat

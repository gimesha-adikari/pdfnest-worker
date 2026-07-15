from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from .models import OfficeOutputFormat
from .service import OfficeConversionService

router = APIRouter(prefix="/api/v1/office", tags=["office"])


@router.post("/convert", response_class=FileResponse)
async def convert_office(
    format: Literal["docx", "xlsx", "pptx"] = Form(...),
    file: UploadFile = File(...),
) -> FileResponse:
    return await OfficeConversionService.convert(format=format, file=file)

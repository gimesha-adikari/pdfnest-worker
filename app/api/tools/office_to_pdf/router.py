from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from .service import OfficeToPdfService


router = APIRouter(prefix="/api/v1/office-to-pdf", tags=["office-to-pdf"])


@router.post("/convert", response_class=FileResponse)
async def convert_office_to_pdf(
    format: Literal["docx", "xlsx", "pptx"] = Form(...),
    file: UploadFile = File(...),
) -> FileResponse:
    return await OfficeToPdfService.convert(format=format, file=file)

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from .models import OfficeOutputFormat
from app.api.tools.ocr.router import create_route_cancellation_checker
from .service import OfficeConversionService

router = APIRouter(prefix="/api/v1/office", tags=["office"])


@router.post("/convert", response_class=FileResponse)
async def convert_office(
    request: Request,
    format: Literal["docx", "xlsx", "pptx"] = Form(...),
    file: UploadFile = File(...),
    lang: str = Form("eng"),
) -> FileResponse:
    check, cleanup = create_route_cancellation_checker(request) if format == "docx" else (None, lambda: None)
    try:
        return await OfficeConversionService.convert(format=format, file=file, language=lang, cancellation_check=check)
    finally:
        cleanup()

from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .service import EditorService
from .utils import cleanup_paths, temp_file_path

router = APIRouter(prefix="/api/v1/editor", tags=["editor"])
service = EditorService()


@router.post("/extract")
async def extract_layout(
    file: UploadFile = File(...),
    file_password: str | None = Form(default=None),
):
    input_fd, input_path = tempfile.mkstemp(prefix="pdfnest-source-", suffix=".pdf")
    os.close(input_fd)

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            result = service.extract_layout(input_path, file_password)
        except Exception:
            traceback.print_exc()
            raise

        result["source_tracker"] = input_path
        return JSONResponse(content=result)
    except HTTPException:
        cleanup_paths(input_path)
        raise
    except Exception as exc:
        cleanup_paths(input_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/compile")
async def compile_layout(
    file: UploadFile = File(...),
    payload: str = Form(...),
):
    input_fd, input_path = tempfile.mkstemp(prefix="pdfnest-source-", suffix=".pdf")
    os.close(input_fd)

    pages_json_path = temp_file_path(prefix="pdfnest-layout-", suffix=".json")
    output_pdf_path = temp_file_path(prefix="pdfnest-output-", suffix=".pdf")

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        with open(pages_json_path, "w", encoding="utf-8") as f:
            f.write(payload)

        service.compile_layout(input_path, output_pdf_path, pages_json_path)
        return FileResponse(
            output_pdf_path,
            media_type="application/pdf",
            filename="edited_" + Path(output_pdf_path).name,
        )
    except HTTPException:
        cleanup_paths(input_path, pages_json_path, output_pdf_path)
        raise
    except Exception as exc:
        cleanup_paths(input_path, pages_json_path, output_pdf_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

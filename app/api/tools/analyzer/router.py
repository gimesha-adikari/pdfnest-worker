from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .document import analyze_document
from .models import PDFAnalysis
from .python_ast import PythonASTRequest, PythonASTResponse, analyze_python_ast

router = APIRouter(
    prefix="/api/v1/analyzer",
    tags=["analyzer"],
)


@router.post(
    "/python-ast",
    response_model=PythonASTResponse,
)
async def analyze_python_repo_ast(payload: PythonASTRequest) -> PythonASTResponse:
    return analyze_python_ast(payload)


@router.post(
    "/analyze",
    response_model=PDFAnalysis,
)
async def analyze_pdf(
        file: UploadFile = File(...),
        file_password: str | None = Form(None),
):
    fd, input_path = tempfile.mkstemp(
        prefix="pdfnest-analyze-",
        suffix=".pdf",
    )
    os.close(fd)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        return analyze_document(
            input_path=input_path,
            password=file_password,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

    finally:
        try:
            os.remove(input_path)
        except FileNotFoundError:
            pass
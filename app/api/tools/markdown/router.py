from __future__ import annotations

import logging
from typing import Any, Optional

import fitz
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.api.tools.markdown.service import convert_pdf_to_markdown

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/markdown", tags=["markdown"])


@router.post("/convert")
async def convert_markdown_sync(
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
    lang: str = Form("eng"),
    include_annotations: bool = Form(False),
    embed_images: bool = Form(False),
) -> JSONResponse:
    """Synchronous endpoint for PDF to Markdown conversion."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing upload file parameter.")

    tmp_input = temp_file_path("md-sync-input-", ".pdf")
    try:
        content = await file.read()
        with open(tmp_input, "wb") as f:
            f.write(content)

        markdown_text = convert_pdf_to_markdown(
            input_path=tmp_input,
            password=password,
            lang=lang,
            include_annotations=include_annotations,
            embed_images=embed_images,
        )

        return JSONResponse(content={
            "success": True,
            "markdown": markdown_text,
        })
    except (ValueError, fitz.FileDataError) as user_err:
        logger.warning(f"PDF to Markdown client error: {user_err}")
        raise HTTPException(status_code=400, detail=str(user_err))
    except Exception as e:
        logger.error(f"Sync PDF to Markdown conversion failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal processing error during PDF conversion.")
    finally:
        cleanup_paths(tmp_input)

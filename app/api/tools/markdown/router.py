from __future__ import annotations

import logging
from typing import Any, Optional

import fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.tools.editor.utils import cleanup_paths, temp_file_path
from app.core.native_operations import convert_pdf_to_markdown
from starlette.concurrency import run_in_threadpool
from app.api.tools.ocr.router import create_route_cancellation_checker
from app.jobs.cancellation import JobCancelledException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/markdown", tags=["markdown"])


@router.post("/convert")
async def convert_markdown_sync(
    request: Request,
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
    lang: str = Form("eng"),
    include_annotations: bool = Form(False),
    embed_images: bool = Form(False),
) -> JSONResponse:
    """Synchronous endpoint for PDF to Markdown conversion."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing upload file parameter.")

    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)
    tmp_input = temp_file_path("md-sync-input-", ".pdf")
    try:
        content = await file.read()
        with open(tmp_input, "wb") as f:
            f.write(content)

        markdown_text = await run_in_threadpool(
            convert_pdf_to_markdown,
            input_path=tmp_input,
            password=password,
            lang=lang,
            include_annotations=include_annotations,
            embed_images=embed_images,
            cancellation_check=check_cancel,
        )

        return JSONResponse(content={
            "success": True,
            "markdown": markdown_text,
        })
    except JobCancelledException as exc:
        raise HTTPException(status_code=499, detail="Markdown conversion cancelled") from exc
    except (ValueError, fitz.FileDataError) as user_err:
        logger.warning(f"PDF to Markdown client error: {user_err}")
        raise HTTPException(status_code=400, detail=str(user_err))
    except Exception as e:
        logger.error(f"Sync PDF to Markdown conversion failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal processing error during PDF conversion.")
    finally:
        cleanup_cancel()
        cleanup_paths(tmp_input)

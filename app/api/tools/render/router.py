from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .service import render_page_to_jpeg_bytes

router = APIRouter(prefix="/api/v1/render", tags=["render"])


@router.post("/page")
async def render_page(
    file: UploadFile = File(...),
    page: int = Form(...),
    dpi: float = Form(144),
):
    try:
        image_bytes = await render_page_to_jpeg_bytes(file=file, page=page, dpi=dpi)
        return Response(content=image_bytes, media_type="image/jpeg")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
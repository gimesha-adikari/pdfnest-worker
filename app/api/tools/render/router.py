from __future__ import annotations

import asyncio
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .limiter import render_limiter
from .service import (
    create_render_session,
    delete_render_session,
    get_render_session_page,
    render_page_to_jpeg_bytes,
)

router = APIRouter(
    prefix="/api/v1/render",
    tags=["render"],
)


@router.get("/metrics")
async def get_render_metrics():
    """
    Observability endpoint for render concurrency and capacity metrics.
    """
    return render_limiter.get_metrics()


@router.post("/page")
async def render_page(
        file: UploadFile = File(...),
        page: int = Form(...),
        dpi: float = Form(144),
        clip_x0: float | None = Form(None),
        clip_y0: float | None = Form(None),
        clip_x1: float | None = Form(None),
        clip_y1: float | None = Form(None),
):
    try:
        image_bytes, timings = await render_limiter.run_with_timings(
            render_page_to_jpeg_bytes(
                file=file,
                page=page,
                dpi=dpi,
                clip_x0=clip_x0,
                clip_y0=clip_y0,
                clip_x1=clip_x1,
                clip_y1=clip_y1,
            )
        )

        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=60",
                "X-Queue-Wait-Ms": f"{timings.get('queue_wait_ms', 0.0):.2f}",
                "X-Render-Exec-Ms": f"{timings.get('render_execution_ms', 0.0):.2f}",
            },
        )

    except HTTPException:
        raise

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_RENDER_REQUEST",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "RENDER_FAILED",
                "message": str(exc),
            },
        ) from exc


@router.post("/sessions")
async def create_session(
        file: UploadFile = File(...),
):
    try:
        session = await create_render_session(file)

        return {
            "session_id": session.session_id,
            "page_count": session.page_count,
            "file_size": session.file_size,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_DOCUMENT",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SESSION_CREATE_FAILED",
                "message": str(exc),
            },
        ) from exc


@router.get("/sessions/{session_id}/page/{page}")
async def render_session_page(
        session_id: str,
        page: int,
        dpi: float = 144,
):
    try:
        image_bytes = await render_limiter.run(
            get_render_session_page(
                session_id=session_id,
                page=page,
                dpi=dpi,
            )
        )

        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=60",
            },
        )

    except HTTPException:
        raise

    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": str(exc),
            },
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_RENDER_REQUEST",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SESSION_RENDER_FAILED",
                "message": str(exc),
            },
        ) from exc


@router.delete("/sessions/{session_id}")
async def delete_session(
        session_id: str,
):
    try:
        deleted = delete_render_session(session_id)

        return {
            "deleted": deleted,
        }

    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SESSION_DELETE_FAILED",
                "message": str(exc),
            },
        ) from exc
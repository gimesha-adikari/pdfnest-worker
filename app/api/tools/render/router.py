from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.core.config import settings
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
    metrics = render_limiter.get_metrics()
    from app.core.config import settings
    from .persistent_pool import get_persistent_render_pool

    pool = get_persistent_render_pool()
    if pool is not None and pool.is_running:
        metrics["persistent_pool"] = pool.get_metrics()
    else:
        metrics["persistent_pool"] = {
            "enabled": settings.enable_persistent_render_pool,
            "configured_workers": settings.persistent_render_pool_size if settings.enable_persistent_render_pool else 0,
            "healthy_workers": 0,
            "available_workers": 0,
            "busy_workers": 0,
            "degraded": settings.enable_persistent_render_pool,
            "total_completed": 0,
            "total_failed": 0,
            "total_restarts": 0,
            "total_recycled": 0,
            "total_fallbacks": 0,
        }
    return metrics


@router.post("/page")
async def render_page(
        request: Request,
        file: UploadFile = File(...),
        page: int = Form(...),
        dpi: float = Form(144),
        clip_x0: float | None = Form(None),
        clip_y0: float | None = Form(None),
        clip_x1: float | None = Form(None),
        clip_y1: float | None = Form(None),
):
    # Gated test-only failure injection (defense-in-depth: requires flag AND non-production environment)
    simulate_crash = False
    simulate_hang = False
    if settings.enable_render_failure_injection and settings.app_env.lower() in ("development", "test", "testing"):
        simulate_crash = request.headers.get("x-test-simulate-crash", "").lower() in ("true", "1")
        simulate_hang = request.headers.get("x-test-simulate-hang", "").lower() in ("true", "1")

    try:
        (image_bytes, child_metrics), timings = await render_limiter.run_with_timings(
            render_page_to_jpeg_bytes(
                file=file,
                page=page,
                dpi=dpi,
                clip_x0=clip_x0,
                clip_y0=clip_y0,
                clip_x1=clip_x1,
                clip_y1=clip_y1,
                simulate_crash=simulate_crash,
                simulate_hang=simulate_hang,
            )
        )

        headers = {
            "Cache-Control": "private, max-age=60",
            "X-Queue-Wait-Ms": f"{timings.get('queue_wait_ms', 0.0):.2f}",
            "X-Render-Exec-Ms": f"{timings.get('render_execution_ms', 0.0):.2f}",
        }
        if child_metrics:
            cpu_val = child_metrics.get("render_cpu_ms", child_metrics.get("total_cpu_ms", 0.0))
            headers["X-Render-Child-Cpu-Ms"] = f"{cpu_val:.2f}"
            headers["X-Render-Child-User-Cpu-Ms"] = f"{child_metrics.get('user_cpu_ms', 0.0):.2f}"
            headers["X-Render-Child-Sys-Cpu-Ms"] = f"{child_metrics.get('sys_cpu_ms', 0.0):.2f}"
            headers["X-Render-Child-Vol-Ctx"] = str(child_metrics.get("vol_ctx", 0))
            headers["X-Render-Child-Invol-Ctx"] = str(child_metrics.get("invol_ctx", 0))
            headers["X-Render-Child-Pid"] = str(child_metrics.get("pid", 0))
            headers["X-Render-Child-Rss-Kb"] = str(child_metrics.get("max_rss_kb", 0))
            headers["X-Render-Child-Runqueue-Wait-Ms"] = f"{child_metrics.get('runqueue_wait_ms', 0.0):.2f}"

        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers=headers,
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
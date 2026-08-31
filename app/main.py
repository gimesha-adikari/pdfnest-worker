from __future__ import annotations

import os
import logging
import warnings
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.broker import broker  # noqa: F401
from app.jobs.router import router as jobs_router

from app.api.tools.pdf_to_office.router import router as office_router
from app.api.tools.render.router import router as render_router
from app.api.tools.editor.router import router as editor_router
from app.api.tools.markup.router import router as markup_router
from app.api.tools.analyzer.router import router as analyzer_router
from app.api.tools.metadata.router import router as metadata_router
from app.api.tools.redact.router import router as redact_router
from app.api.tools.sign.router import router as sign_router
from app.api.landing.router import router as landing_router
from app.api.tools.ocr.router import router as ocr_router
from app.api.tools.markdown.router import router as markdown_router
from app.api.ocr_v2.router import router as ocr_v2_router
from app.api.ocr_v2.jobs import router as ocr_v2_jobs_router

import os


APP_NAME = "Platen PDF Worker"
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
APP_ENV = os.getenv("APP_ENV", "development")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
]


from app.core.janitor import start_worker_janitor
from app.core.config import remote_storage_enabled, settings, validate_runtime_config
from app.api.tools.render.persistent_pool import (
    start_persistent_render_pool,
    shutdown_persistent_render_pool,
)

validate_runtime_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[{APP_NAME}] starting in {APP_ENV} mode")
    if not remote_storage_enabled():
        local_root = get_local_storage_dir()
        os.makedirs(local_root, exist_ok=True)
        print(f"[{APP_NAME}] local storage enabled at {local_root}")
    start_worker_janitor()
    if settings.enable_persistent_render_pool:
        print(f"[{APP_NAME}] initializing persistent render worker pool")
        await start_persistent_render_pool()
    yield
    print(f"[{APP_NAME}] shutting down")
    if settings.enable_persistent_render_pool:
        await shutdown_persistent_render_pool()


from app.core.security import WorkerAuthMiddleware

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(WorkerAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "status": "running",
    }


from fastapi.responses import JSONResponse
import shutil
import redis

from app.core.storage import get_local_storage_dir, get_r2_client

logger = logging.getLogger(__name__)

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
async def ready() -> JSONResponse:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_healthy = False
    actor_healthy = True
    r2_healthy = True
    try:
        r = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        redis_healthy = bool(r.ping())
        if settings.actor_heartbeat_required:
            heartbeat = r.get(settings.actor_heartbeat_key)
            actor_healthy = bool(heartbeat)
    except Exception:
        redis_healthy = False
        actor_healthy = False if settings.actor_heartbeat_required else True

    remote_required = remote_storage_enabled()
    if remote_required:
        try:
            get_r2_client().head_bucket(Bucket=settings.r2_bucket)
        except Exception as exc:
            r2_healthy = False
            logger.warning("[WORKER READINESS] R2 bucket metadata check failed: %s", exc)

    binaries = {
        "tesseract": shutil.which("tesseract") is not None,
        "ghostscript": shutil.which("gs") is not None,
        "libreoffice": (
            shutil.which("soffice") is not None
            or shutil.which("libreoffice") is not None
        ),
    }

    engines_healthy = all(binaries.values())
    if not redis_healthy or not actor_healthy or not engines_healthy or (remote_required and not r2_healthy):
        if not redis_healthy:
            logger.warning("[WORKER READINESS] Redis is not ready")
        if not actor_healthy:
            logger.warning("[WORKER READINESS] actor heartbeat missing or stale")
        if not engines_healthy:
            logger.warning("[WORKER READINESS] required document engines are not ready: %s", binaries)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "redis": redis_healthy,
                "binaries": binaries,
                "r2": r2_healthy,
                "actor": actor_healthy,
                "reason": "Worker dependency is unreachable",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "redis": True,
            "r2": r2_healthy,
            "actor": actor_healthy,
            "binaries": binaries,
        },
    )


app.include_router(jobs_router)
app.include_router(office_router)
app.include_router(render_router)
app.include_router(editor_router)
app.include_router(markup_router)
app.include_router(analyzer_router)
app.include_router(metadata_router)
app.include_router(redact_router)
app.include_router(sign_router)
app.include_router(landing_router)
app.include_router(ocr_router)
app.include_router(markdown_router)
app.include_router(ocr_v2_router)
app.include_router(ocr_v2_jobs_router)

warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    module="camelot",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=APP_ENV == "development",
    )

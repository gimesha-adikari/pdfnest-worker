from __future__ import annotations

import os
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

APP_NAME = "PDFNest Worker"
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
APP_ENV = os.getenv("APP_ENV", "development")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[{APP_NAME}] starting in {APP_ENV} mode")
    yield
    print(f"[{APP_NAME}] shutting down")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
)

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


app.include_router(jobs_router)
app.include_router(office_router)
app.include_router(render_router)
app.include_router(editor_router)
app.include_router(markup_router)
app.include_router(analyzer_router)

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
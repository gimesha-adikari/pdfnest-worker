from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["landing"])

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)

_STARTED_AT = datetime.now(timezone.utc)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


def _format_uptime(total_seconds: int) -> str:
    total_seconds = max(0, total_seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def _build_context(request: Request) -> dict[str, object]:
    backend_url = _env("BACKEND_URL")
    if not backend_url:
        backend_url = _normalize_url(str(request.base_url))

    frontend_url = _env("FRONTEND_URL")
    worker_url = _env("WORKER_URL")

    app_name = _env("APP_NAME", "Platen PDF Worker")
    app_version = _env("APP_VERSION", "0.1.0")
    app_env = _env("APP_ENV", "development")

    now = datetime.now(timezone.utc)
    uptime_seconds = int((now - _STARTED_AT).total_seconds())

    status_label = _env("WORKER_STATUS_LABEL")
    status_dot_class = _env("WORKER_STATUS_DOT_CLASS")

    return {
        "request": request,
        "AppName": app_name,
        "AppVersion": app_version,
        "AppEnv": app_env,
        "FrontendURL": frontend_url,
        "BackendURL": _normalize_url(backend_url),
        "HealthURL": f"{_normalize_url(worker_url)}/health",
        "LiveURL": f"{_normalize_url(worker_url)}/health/live",
        "ReadyURL": f"{_normalize_url(worker_url)}/health/ready",
        "CurrentYear": now.year,
        "UptimeLabel": _format_uptime(uptime_seconds),
        "StatusLabel": status_label or None,
        "StatusDotClass": status_dot_class or None,
    }


@router.get("/home", response_class=HTMLResponse, include_in_schema=False)
async def landing(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context=_build_context(request),
    )
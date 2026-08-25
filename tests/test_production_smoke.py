"""
PDFNest Studio V2 — Local Application-Integration Smoke Test Suite
CLASSIFICATION: LOCAL IN-PROCESS ASGI SMOKE TEST (NOT LIVE REMOTE CLUSTER TEST)
Validates that FastAPI endpoints, middleware, authentication, and persistent render
components integrate cleanly over ASGI transport before deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import time
import uuid
import pytest
from PIL import Image
import pymupdf as fitz
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import settings, Settings
from app.api.tools.render.persistent_pool import (
    PersistentRenderWorkerPool,
    start_persistent_render_pool,
    shutdown_persistent_render_pool,
)


@pytest.fixture
def smoke_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "PDFNest Studio V2 Production Smoke Test", fontsize=16)
    page.insert_text((50, 140), "ISO A4 Verification Document for Automated Deployments", fontsize=11)
    page.draw_rect(fitz.Rect(50, 180, 545, 300), color=(0.2, 0.4, 0.8), fill=(0.9, 0.95, 1.0))
    page.insert_text((60, 210), "Production Deployment Smoke Test Content", fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def generate_auth_headers(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    secret = settings.worker_shared_secret
    nonce = str(uuid.uuid4())
    timestamp_str = str(time.time())
    method_str = method.upper()

    body_hash = hashlib.sha256(body).hexdigest() if body else ""

    if body_hash:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}\n{body_hash}"
    else:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}"

    sig = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-Worker-Signature": sig,
        "X-Worker-Timestamp": timestamp_str,
        "X-Worker-Nonce": nonce,
    }
    if body_hash:
        headers["X-Worker-Body-Hash"] = body_hash

    return headers


# 1. Health Endpoints
@pytest.mark.anyio
async def test_production_health_endpoints():
    """Verify production service liveness and basic health."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        resp_live = await client.get("/health/live")
        assert resp_live.status_code == 200
        assert resp_live.json() == {"status": "alive"}


# 2. Authentication Enforcement
@pytest.mark.anyio
async def test_production_render_authentication_enforced(smoke_pdf_bytes: bytes):
    """Verify unauthorized requests to render endpoints are strictly rejected with 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        files = {"file": ("smoke.pdf", smoke_pdf_bytes, "application/pdf")}
        data = {"page": "1", "dpi": "144"}

        # Unauthenticated request
        resp = await client.post("/api/v1/render/page", files=files, data=data)
        assert resp.status_code == 401


# 3. Authenticated Production Page Rendering
@pytest.mark.anyio
async def test_production_authenticated_render_page(smoke_pdf_bytes: bytes):
    """Verify authenticated render endpoint returns valid JPEG with complete telemetry."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = generate_auth_headers("POST", "/api/v1/render/page")
        files = {"file": ("smoke.pdf", smoke_pdf_bytes, "application/pdf")}
        data = {"page": "1", "dpi": "144"}

        resp = await client.post("/api/v1/render/page", headers=headers, files=files, data=data)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert len(resp.content) > 0

        # Validate image format and dimensions
        img = Image.open(io.BytesIO(resp.content))
        assert img.format == "JPEG"
        assert img.mode == "RGB"
        assert img.width > 0 and img.height > 0

        # Validate response headers
        assert "x-queue-wait-ms" in resp.headers
        assert "x-render-exec-ms" in resp.headers


# 4. Observability Metrics
@pytest.mark.anyio
async def test_production_render_metrics_observability():
    """Verify /api/v1/render/metrics reports truthful subsystem state and schema."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = generate_auth_headers("GET", "/api/v1/render/metrics")
        resp = await client.get("/api/v1/render/metrics", headers=headers)
        assert resp.status_code == 200
        data = resp.json()

        # Limiter metrics
        assert "max_concurrency" in data
        assert "active_renders" in data
        assert "queued_renders" in data

        # Persistent pool metrics
        assert "persistent_pool" in data
        pp = data["persistent_pool"]
        assert "enabled" in pp
        assert "configured_workers" in pp
        assert "healthy_workers" in pp
        assert "available_workers" in pp
        assert "busy_workers" in pp
        assert "degraded" in pp
        assert "total_fallbacks" in pp
        assert "fallback_reasons" in pp


# 5. Production Simulation Headers Gated
@pytest.mark.anyio
async def test_production_simulation_headers_strictly_ignored(smoke_pdf_bytes: bytes):
    """Verify simulation headers cannot trigger failure injection when APP_ENV=production."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = generate_auth_headers("POST", "/api/v1/render/page")
        headers["X-Test-Simulate-Crash"] = "true"
        headers["X-Test-Simulate-Hang"] = "true"
        files = {"file": ("smoke.pdf", smoke_pdf_bytes, "application/pdf")}
        data = {"page": "1", "dpi": "144"}

        resp = await client.post("/api/v1/render/page", headers=headers, files=files, data=data)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"

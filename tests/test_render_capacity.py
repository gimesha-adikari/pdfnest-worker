from __future__ import annotations

import asyncio
import io
import time
import pytest
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.api.tools.render.limiter import RenderConcurrencyLimiter, render_limiter
import app.api.tools.render.router as render_router
from tests.test_security import generate_headers
from tests.test_render_sessions import _create_sample_pdf_bytes

client = TestClient(app)


@pytest.mark.anyio
async def test_limiter_concurrency_enforcement():
    limiter = RenderConcurrencyLimiter(max_concurrency=2, max_queue=10, queue_timeout=5.0)
    
    max_observed_concurrent = 0
    active_now = 0
    lock = asyncio.Lock()

    async def worker_task():
        nonlocal max_observed_concurrent, active_now
        async with limiter.acquire():
            async with lock:
                active_now += 1
                if active_now > max_observed_concurrent:
                    max_observed_concurrent = active_now
            await asyncio.sleep(0.05)
            async with lock:
                active_now -= 1

    tasks = [asyncio.create_task(worker_task()) for _ in range(8)]
    await asyncio.gather(*tasks)

    assert max_observed_concurrent == 2, f"Expected max 2 concurrent renders, observed {max_observed_concurrent}"
    metrics = limiter.get_metrics()
    assert metrics["total_completed"] == 8
    assert metrics["active_renders"] == 0
    assert metrics["queued_renders"] == 0


@pytest.mark.anyio
async def test_limiter_execution_timeout():
    limiter = RenderConcurrencyLimiter(max_concurrency=1, max_queue=5, render_timeout=0.05)

    async def stalled_render():
        await asyncio.sleep(0.2)
        return b"fake_jpeg_bytes"

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await limiter.run(stalled_render())

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail["code"] == "RENDER_TIMEOUT"
    assert limiter.total_renders_timed_out == 1
    assert limiter.active_renders == 0

    # Verify next request acquires immediately
    async def fast_render():
        return b"fast_bytes"

    result = await limiter.run(fast_render())
    assert result == b"fast_bytes"
    assert limiter.active_renders == 0


@pytest.mark.anyio
async def test_limiter_queue_saturation_rejection():
    limiter = RenderConcurrencyLimiter(max_concurrency=1, max_queue=2, queue_timeout=5.0)

    async def slow_task():
        async with limiter.acquire():
            await asyncio.sleep(0.1)

    t1 = asyncio.create_task(slow_task())
    await asyncio.sleep(0.01)

    t2 = asyncio.create_task(slow_task())
    t3 = asyncio.create_task(slow_task())
    await asyncio.sleep(0.01)

    assert limiter.active_renders == 1
    assert limiter.queued_renders == 2

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        async with limiter.acquire():
            pass

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "RENDER_QUEUE_FULL"
    assert limiter.total_renders_rejected == 1

    await asyncio.gather(t1, t2, t3)
    assert limiter.active_renders == 0


@pytest.mark.anyio
async def test_limiter_queue_timeout():
    limiter = RenderConcurrencyLimiter(max_concurrency=1, max_queue=5, queue_timeout=0.05)

    async def blocking_task():
        async with limiter.acquire():
            await asyncio.sleep(0.2)

    t1 = asyncio.create_task(blocking_task())
    await asyncio.sleep(0.01)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        async with limiter.acquire():
            pass

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "RENDER_QUEUE_TIMEOUT"

    await t1
    assert limiter.active_renders == 0


@pytest.mark.anyio
async def test_limiter_cancellation_releases_slot():
    limiter = RenderConcurrencyLimiter(max_concurrency=1, max_queue=5)

    async def cancelled_task():
        async with limiter.acquire():
            await asyncio.sleep(1.0)

    task = asyncio.create_task(cancelled_task())
    await asyncio.sleep(0.02)
    assert limiter.active_renders == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.active_renders == 0
    assert limiter.total_renders_failed == 1

    acquired = False
    async with limiter.acquire():
        acquired = True
    assert acquired is True
    assert limiter.active_renders == 0


@pytest.mark.anyio
async def test_http_endpoint_concurrency_enforcement(monkeypatch):
    """
    Real HTTP integration test against FastAPI /api/v1/render/page endpoint.
    Verifies that concurrent HTTP requests never exceed configured concurrency.
    """
    # Configure global limiter with max_concurrency=2
    render_limiter.max_concurrency = 2
    render_limiter._semaphore = asyncio.Semaphore(2)
    render_limiter.reset_metrics()

    max_active_observed = 0
    current_active = 0
    active_lock = asyncio.Lock()

    orig_render = render_router.render_page_to_jpeg_bytes

    async def instrumented_render(*args, **kwargs):
        nonlocal max_active_observed, current_active
        async with active_lock:
            current_active += 1
            if current_active > max_active_observed:
                max_active_observed = current_active
        try:
            await asyncio.sleep(0.05)
            return await orig_render(*args, **kwargs)
        finally:
            async with active_lock:
                current_active -= 1

    monkeypatch.setattr(render_router, "render_page_to_jpeg_bytes", instrumented_render)

    pdf_bytes = _create_sample_pdf_bytes(num_pages=1)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        async def send_render_request():
            headers = generate_headers("POST", "/api/v1/render/page")
            files = {"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
            data = {"page": "1", "dpi": "72.0"}
            resp = await ac.post("/api/v1/render/page", headers=headers, files=files, data=data)
            return resp

        tasks = [send_render_request() for _ in range(6)]
        responses = await asyncio.gather(*tasks)

    for r in responses:
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        assert r.headers["content-type"] == "image/jpeg"

    assert max_active_observed == 2, f"Expected max 2 concurrent active renders at HTTP layer, got {max_active_observed}"
    metrics = render_limiter.get_metrics()
    assert metrics["active_renders"] == 0
    assert metrics["total_completed"] >= 6





@pytest.mark.anyio
async def test_http_endpoint_underlying_subprocess_termination_on_timeout(monkeypatch):
    """
    Real HTTP integration test proving that on execution timeout:
    1. Child render process exists.
    2. HTTP 504 is returned.
    3. Child process is terminated and reaped (no orphan remains).
    4. Limiter slot is released.
    5. Subsequent render succeeds.
    """
    import os
    import signal
    import sys
    from app.api.tools.render import service

    orig_timeout = render_limiter.render_timeout
    render_limiter.render_timeout = 0.15
    render_limiter.reset_metrics()

    spawned_pid = None
    orig_create_subprocess = asyncio.create_subprocess_exec

    try:
        async def slow_create_subprocess(*args, **kwargs):
            nonlocal spawned_pid
            slow_cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
            proc = await orig_create_subprocess(*slow_cmd, **kwargs)
            spawned_pid = proc.pid
            return proc

        monkeypatch.setattr(service.asyncio, "create_subprocess_exec", slow_create_subprocess)

        pdf_bytes = _create_sample_pdf_bytes(num_pages=1)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            headers = generate_headers("POST", "/api/v1/render/page")
            files = {"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
            data = {"page": "1", "dpi": "72.0"}

            # 1. Send request that will time out
            resp = await ac.post("/api/v1/render/page", headers=headers, files=files, data=data)

            # 2. Verify HTTP 504
            assert resp.status_code == 504, f"Got {resp.status_code}: {resp.text}"
            assert resp.json()["detail"]["code"] == "RENDER_TIMEOUT"

            # 3. Verify child process existed and is now TERMINATED (no orphan process)
            assert spawned_pid is not None, "Child process was never spawned"
            with pytest.raises(ProcessLookupError):
                os.kill(spawned_pid, 0)

            # 4. Verify limiter slot was released
            assert render_limiter.active_renders == 0
            assert render_limiter.total_renders_timed_out == 1

            # 5. Restore normal execution and verify subsequent render succeeds
            render_limiter.render_timeout = orig_timeout
            monkeypatch.undo()

            headers2 = generate_headers("POST", "/api/v1/render/page")
            files2 = {"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
            resp2 = await ac.post("/api/v1/render/page", headers=headers2, files=files2, data=data)
            assert resp2.status_code == 200
            assert resp2.headers["content-type"] == "image/jpeg"
            assert len(resp2.content) > 0
            assert render_limiter.active_renders == 0
    finally:
        render_limiter.render_timeout = orig_timeout
        render_limiter.reset_metrics()


def test_render_api_metrics_endpoint():
    headers = generate_headers("GET", "/api/v1/render/metrics")
    resp = client.get("/api/v1/render/metrics", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "max_concurrency" in data
    assert "active_renders" in data
    assert "queued_renders" in data
    assert "total_completed" in data
    assert "total_rejected" in data
    assert "total_timed_out" in data


def test_cgroup_memory_detection_v2(monkeypatch):
    from app.api.tools.render import limiter

    monkeypatch.delenv("RENDER_CONCURRENCY_LIMIT", raising=False)
    monkeypatch.setattr(limiter.os.path, "exists", lambda p: p == "/sys/fs/cgroup/memory.max")
    
    from unittest.mock import mock_open
    m = mock_open(read_data="1073741824\n") # 1 GB
    monkeypatch.setattr("builtins.open", m)
    
    limit = limiter.get_default_concurrency_limit()
    # 1 GB / 512 MB = 2 slots
    assert limit == 2


def test_cgroup_memory_detection_max_fallback(monkeypatch):
    from app.api.tools.render import limiter

    monkeypatch.delenv("RENDER_CONCURRENCY_LIMIT", raising=False)
    monkeypatch.setattr(limiter.os.path, "exists", lambda p: p == "/sys/fs/cgroup/memory.max")
    
    from unittest.mock import mock_open
    m = mock_open(read_data="max\n")
    monkeypatch.setattr("builtins.open", m)
    
    limit = limiter.get_default_concurrency_limit()
    assert 1 <= limit <= 8


def test_cgroup_memory_detection_v1(monkeypatch):
    from app.api.tools.render import limiter

    monkeypatch.delenv("RENDER_CONCURRENCY_LIMIT", raising=False)
    monkeypatch.setattr(limiter.os.path, "exists", lambda p: p == "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    
    from unittest.mock import mock_open
    m = mock_open(read_data="536870912\n") # 512 MB
    monkeypatch.setattr("builtins.open", m)
    
    limit = limiter.get_default_concurrency_limit()
    # 512 MB / 512 MB = 1 slot
    assert limit == 1


def test_huge_page_clip_rendering():
    import pymupdf as fitz
    from PIL import Image

    doc = fitz.open()
    # 48 x 36 inch page (3456 x 2592 pt)
    page = doc.new_page(width=3456, height=2592)
    # Draw green marker at (1000, 1000, 1064, 1064) pt
    page.draw_rect(fitz.Rect(1000, 1000, 1064, 1064), color=(0, 1, 0), fill=(0, 1, 0))
    pdf_bytes = doc.tobytes()
    doc.close()

    from app.api.tools.render.renderer import render_pdf_page_to_jpeg

    # Render at Scale 8.0 (576 DPI, zoom = 8.0) using clip coordinates
    # 64pt * 8 = 512px
    jpeg_bytes = render_pdf_page_to_jpeg(
        pdf_bytes=pdf_bytes,
        page_number=1,
        dpi=576.0,
        clip_x0=1000.0,
        clip_y0=1000.0,
        clip_x1=1064.0,
        clip_y1=1064.0,
    )

    img = Image.open(io.BytesIO(jpeg_bytes))
    assert img.width == 512
    assert img.height == 512

    # Check green pixel inside marker
    r, g, b = img.getpixel((100, 100))
    assert g > 200 and r < 50 and b < 50

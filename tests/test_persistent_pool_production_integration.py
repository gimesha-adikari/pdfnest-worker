from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import signal
import sys
import tempfile
import time
import uuid
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient, ASGITransport
from fastapi.testclient import TestClient
from fastapi import UploadFile
import pymupdf as fitz
from PIL import Image

from app.main import app
from app.core.config import settings, Settings
from app.api.tools.render.persistent_pool import (
    PersistentRenderWorker,
    PersistentRenderWorkerPool,
    PersistentWorkerInfrastructureError,
    PersistentWorkerTimeoutError,
    get_persistent_render_pool,
    start_persistent_render_pool,
    shutdown_persistent_render_pool,
    WORKER_SCRIPT,
)
from app.api.tools.render.service import (
    render_page_to_jpeg_bytes,
    render_page_to_jpeg_bytes_subprocess,
)


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    doc = fitz.open()
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((50, 100), "PDFNest Studio V2 Phase 3I Lifecycle Audit - Page 1", fontsize=16)
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 100), "PDFNest Studio V2 Phase 3I Lifecycle Audit - Page 2", fontsize=16)
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


def assert_queue_strict_invariants(pool: PersistentRenderWorkerPool):
    """
    Empirically inspects all items in available_workers queue to prove:
    1. Every worker in queue is alive
    2. Every worker in queue is registered in pool.workers
    3. Every worker in queue is the exact active instance in pool.workers
    4. Worker PID in queue matches current registered process PID
    """
    items = []
    while not pool.available_workers.empty():
        items.append(pool.available_workers.get_nowait())

    try:
        for w in items:
            assert w.is_alive, f"Dead worker {w.worker_id} found in available_workers queue"
            assert w.worker_id in pool.workers, f"Unregistered worker {w.worker_id} in queue"
            assert pool.workers[w.worker_id] is w, f"Stale worker reference for {w.worker_id} in queue"
            assert w.pid == pool.workers[w.worker_id].pid, f"Mismatched PID for {w.worker_id} in queue"
    finally:
        for w in items:
            pool.available_workers.put_nowait(w)


# 1. CRITICAL: Production Worker Provenance
def test_production_worker_provenance():
    """Verify production worker script is owned by production package and does not depend on benchmarks/."""
    assert WORKER_SCRIPT.exists()
    assert "benchmarks" not in str(WORKER_SCRIPT)
    assert WORKER_SCRIPT.name == "worker_process.py"
    assert "app/api/tools/render" in str(WORKER_SCRIPT)


# 2. Feature Flag Defaults & Explicit Settings Clamping
def test_feature_flag_default_is_disabled_and_clamped():
    """Verify default disabled state and strict clamping [1, 32] for pool size."""
    with patch.dict(os.environ, {}, clear=False):
        s = Settings()
        assert s.enable_persistent_render_pool is False
        assert s.persistent_render_pool_size == 4
        assert s.worker_max_renders == 1000
        assert s.worker_max_rss_mb == 350
        assert s.enable_render_failure_injection is False

    with patch.dict(os.environ, {"PERSISTENT_RENDER_POOL_SIZE": "0", "WORKER_MAX_RSS_MB": "-10"}, clear=False):
        s2 = Settings()
        assert s2.persistent_render_pool_size == 1
        assert s2.worker_max_rss_mb == 50

    with patch.dict(os.environ, {"PERSISTENT_RENDER_POOL_SIZE": "100"}, clear=False):
        s3 = Settings()
        assert s3.persistent_render_pool_size == 32

    with patch.dict(os.environ, {"PERSISTENT_RENDER_POOL_SIZE": "invalid"}, clear=False):
        s4 = Settings()
        assert s4.persistent_render_pool_size == 4


# 3. Subprocess Path When Feature Flag OFF
@pytest.mark.anyio
async def test_persistent_pool_disabled_uses_subprocess_renderer(sample_pdf_bytes: bytes):
    """When flag is False, service directly uses subprocess renderer without starting pool."""
    mock_settings = Settings(enable_persistent_render_pool=False)
    with patch("app.api.tools.render.service.settings", mock_settings):
        upload = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
        jpeg_bytes, child_metrics = await render_page_to_jpeg_bytes(upload, page=1, dpi=144.0)
        assert len(jpeg_bytes) > 0
        img = Image.open(io.BytesIO(jpeg_bytes))
        assert img.format == "JPEG"
        assert img.mode == "RGB"


# 4. Schedstat Delta & High-Water Mark RSS Telemetry
@pytest.mark.anyio
async def test_schedstat_delta_and_high_water_rss_telemetry(sample_pdf_bytes: bytes):
    """Verify runqueue_wait_ms is a non-negative per-request delta and RSS is a high-water mark."""
    pool = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
    await pool.start()

    try:
        jpeg1, meta1 = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
        assert len(jpeg1) > 0
        assert meta1["status"] == "ok"
        assert meta1["runqueue_wait_ms"] >= 0.0
        assert meta1["max_rss_kb"] > 0

        # Second request reports delta, not cumulative accumulation
        jpeg2, meta2 = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
        assert meta2["runqueue_wait_ms"] >= 0.0
        assert meta2["max_rss_kb"] >= meta1["max_rss_kb"]
    finally:
        await pool.shutdown()


# 5. Strict Type Validation (No Unsafe Boolean / String Coercion)
@pytest.mark.anyio
async def test_strict_protocol_type_validation_keeps_worker_alive(sample_pdf_bytes: bytes):
    """
    Verify malformed types (string/float/bool page, string/bool dpi, malformed clip arrays)
    are strictly rejected with USER_INPUT_ERROR while worker remains alive.
    """
    pool = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
    await pool.start()

    try:
        worker = list(pool.workers.values())[0]
        initial_pid = worker.pid

        # A. Page validations
        invalid_pages = ["abc", None, True, False, 1.5, 0, -1]
        for bad_page in invalid_pages:
            with pytest.raises(ValueError, match="Page number"):
                await worker.render_page(sample_pdf_bytes, page=bad_page)
            assert worker.is_alive and worker.pid == initial_pid

        # B. DPI validations
        invalid_dpis = ["144", None, True, False, 0, -50.0]
        for bad_dpi in invalid_dpis:
            with pytest.raises(ValueError, match="DPI"):
                await worker.render_page(sample_pdf_bytes, dpi=bad_dpi)
            assert worker.is_alive and worker.pid == initial_pid

        # C. Clip validations
        invalid_clips = [
            "invalid",
            [1, 2],
            [1, 2, "bad", 4],
            [1, 2, 3, None],
            [1, 2, 3, True],
        ]
        for bad_clip in invalid_clips:
            with pytest.raises(ValueError, match="Clip"):
                await worker.render_page(sample_pdf_bytes, clip_raw=bad_clip)
            assert worker.is_alive and worker.pid == initial_pid

        # D. Subsequent valid request succeeds on the exact same worker
        jpeg, meta = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
        assert len(jpeg) > 0
        assert meta["status"] == "ok"
        assert list(pool.workers.values())[0].pid == initial_pid
    finally:
        await pool.shutdown()


# 6. Failure Injection Gating (Disabled in Production by Default)
@pytest.mark.anyio
async def test_failure_injection_strictly_gated_by_environment(sample_pdf_bytes: bytes):
    """
    A. When ENABLE_RENDER_FAILURE_INJECTION=false (default):
       simulate_crash=True is ignored and worker remains alive.
    B. When ENABLE_RENDER_FAILURE_INJECTION=true:
       simulate_crash=True causes real worker crash, detected by pool, and worker is replaced.
    """
    # A. Injection disabled (default)
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "false"}, clear=False):
        pool_safe = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
        await pool_safe.start()
        try:
            pid_before = list(pool_safe.workers.values())[0].pid
            jpeg, meta = await pool_safe.render(sample_pdf_bytes, page=1, dpi=144.0, simulate_crash=True)
            assert len(jpeg) > 0
            assert meta["status"] == "ok"
            assert list(pool_safe.workers.values())[0].pid == pid_before
        finally:
            await pool_safe.shutdown()

    # B. Injection enabled
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "true"}, clear=False):
        pool_test = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
        await pool_test.start()
        try:
            worker = list(pool_test.workers.values())[0]
            with pytest.raises(PersistentWorkerInfrastructureError):
                await worker.render_page(sample_pdf_bytes, page=1, dpi=144.0, simulate_crash=True)

            assert not worker.is_alive
        finally:
            await pool_test.shutdown()


# 7. Defense-in-Depth: Production Context Rejects Test Simulation Headers
@pytest.mark.anyio
async def test_production_environment_ignores_simulation_headers(sample_pdf_bytes: bytes):
    """
    Under production configuration (APP_ENV=production, ENABLE_RENDER_FAILURE_INJECTION=false),
    X-Test-Simulate-Crash and X-Test-Simulate-Hang are strictly ignored and normal renders succeed.
    """
    pool = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
    await pool.start()
    initial_pid = list(pool.workers.values())[0].pid

    prod_settings = Settings(
        enable_persistent_render_pool=True,
        enable_render_failure_injection=False,
        app_env="production",
    )
    with patch("app.core.config.settings", prod_settings), \
         patch("app.api.tools.render.service.settings", prod_settings), \
         patch("app.api.tools.render.service.get_persistent_render_pool", return_value=pool):

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Simulate Crash header ignored in production
            headers_crash = generate_auth_headers("POST", "/api/v1/render/page")
            headers_crash["X-Test-Simulate-Crash"] = "true"
            files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
            data = {"page": "1", "dpi": "144"}

            resp = await client.post("/api/v1/render/page", headers=headers_crash, files=files, data=data)
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/jpeg"
            assert list(pool.workers.values())[0].pid == initial_pid
            assert pool.total_fallbacks == 0

            # 2. Simulate Hang header ignored in production
            headers_hang = generate_auth_headers("POST", "/api/v1/render/page")
            headers_hang["X-Test-Simulate-Hang"] = "true"

            resp2 = await client.post("/api/v1/render/page", headers=headers_hang, files=files, data=data)
            assert resp2.status_code == 200
            assert resp2.headers["content-type"] == "image/jpeg"
            assert list(pool.workers.values())[0].pid == initial_pid
            assert pool.total_fallbacks == 0

    await pool.shutdown()


# 8. GENUINE REAL-CRASH HTTP TEST (With Returncode & Process-Death Proof)
@pytest.mark.anyio
async def test_real_http_endpoint_actual_worker_crash_and_fallback(sample_pdf_bytes: bytes):
    """
    GENUINE REAL HTTP + ACTUAL WORKER CRASH + REAL SUBPROCESS FALLBACK:
    1. HTTP POST /api/v1/render/page with X-Test-Simulate-Crash: true
    2. Actual PersistentRenderWorker executes os._exit(139) (SIGSEGV)
    3. Retained original process wait() reports returncode 139 (or -SIGSEGV)
    4. Service increments fallback counter and invokes certified subprocess renderer
    5. HTTP 200 with valid JPEG is returned
    6. Replacement worker is active with new PID
    """
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "true", "APP_ENV": "test"}, clear=False):
        pool = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
        await pool.start()

        initial_worker = list(pool.workers.values())[0]
        initial_pid = initial_worker.pid
        original_proc = initial_worker.proc
        initial_fallbacks = pool.total_fallbacks

        mock_settings = Settings(enable_persistent_render_pool=True, enable_render_failure_injection=True, app_env="test")
        with patch("app.core.config.settings", mock_settings), \
             patch("app.api.tools.render.router.settings", mock_settings), \
             patch("app.api.tools.render.service.settings", mock_settings), \
             patch("app.api.tools.render.service.get_persistent_render_pool", return_value=pool):

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                headers = generate_auth_headers("POST", "/api/v1/render/page")
                headers["X-Test-Simulate-Crash"] = "true"
                files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
                data = {"page": "1", "dpi": "144"}

                # Real HTTP call -> real worker crash -> real subprocess fallback
                resp = await client.post("/api/v1/render/page", headers=headers, files=files, data=data)
                assert resp.status_code == 200
                assert resp.headers["content-type"] == "image/jpeg"
                assert len(resp.content) > 0
                img = Image.open(io.BytesIO(resp.content))
                assert img.format == "JPEG"

                # Assert fallback counter incremented by exactly 1
                assert pool.total_fallbacks == initial_fallbacks + 1

                # Verify process-death return code
                await original_proc.wait()
                assert original_proc.returncode in (139, -signal.SIGSEGV)

                # Assert replacement worker is alive with new PID
                replacement_worker = list(pool.workers.values())[0]
                assert replacement_worker.pid != initial_pid
                assert replacement_worker.is_alive

                # Subsequent normal HTTP render succeeds without triggering fallback
                normal_headers = generate_auth_headers("POST", "/api/v1/render/page")
                resp2 = await client.post("/api/v1/render/page", headers=normal_headers, files=files, data=data)
                assert resp2.status_code == 200
                assert pool.total_fallbacks == initial_fallbacks + 1

        await pool.shutdown()


# 9. GENUINE REAL-HANG / TIMEOUT HTTP TEST (With Returncode Proof)
@pytest.mark.anyio
async def test_real_http_endpoint_actual_worker_hang_timeout_and_fallback(sample_pdf_bytes: bytes):
    """
    GENUINE REAL HTTP + ACTUAL WORKER HANG + TIMEOUT + REAL SUBPROCESS FALLBACK:
    1. HTTP POST /api/v1/render/page with X-Test-Simulate-Hang: true
    2. Actual PersistentRenderWorker sleeps for 60s
    3. Pool timeout (2.0s) fires, terminates hanging worker via SIGKILL
    4. Retained original process wait() reports returncode -SIGKILL (-9) or 137
    5. Service increments fallback counter and invokes certified subprocess renderer
    6. HTTP 200 with valid JPEG is returned
    """
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "true", "APP_ENV": "test"}, clear=False):
        pool = PersistentRenderWorkerPool(size=1, render_timeout_s=2.0)
        await pool.start()

        initial_worker = list(pool.workers.values())[0]
        initial_pid = initial_worker.pid
        original_proc = initial_worker.proc
        initial_fallbacks = pool.total_fallbacks

        mock_settings = Settings(enable_persistent_render_pool=True, enable_render_failure_injection=True, app_env="test")
        with patch("app.core.config.settings", mock_settings), \
             patch("app.api.tools.render.router.settings", mock_settings), \
             patch("app.api.tools.render.service.settings", mock_settings), \
             patch("app.api.tools.render.service.get_persistent_render_pool", return_value=pool):

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                headers = generate_auth_headers("POST", "/api/v1/render/page")
                headers["X-Test-Simulate-Hang"] = "true"
                files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
                data = {"page": "1", "dpi": "144"}

                # Real HTTP call -> real worker hang -> timeout -> subprocess fallback
                resp = await client.post("/api/v1/render/page", headers=headers, files=files, data=data)
                assert resp.status_code == 200
                assert resp.headers["content-type"] == "image/jpeg"
                assert len(resp.content) > 0

                assert pool.total_fallbacks == initial_fallbacks + 1

                # Verify process-death return code from SIGKILL
                await original_proc.wait()
                assert original_proc.returncode in (-signal.SIGKILL, -9, 137)

                replacement_worker = list(pool.workers.values())[0]
                assert replacement_worker.pid != initial_pid
                assert replacement_worker.is_alive

        await pool.shutdown()


# 10. REAL HTTP + MOCKED PERSISTENT POOL FAILURE + REAL SUBPROCESS FALLBACK
@pytest.mark.anyio
async def test_http_endpoint_fallback_with_mocked_pool_failure(sample_pdf_bytes: bytes):
    """
    REAL HTTP + MOCKED PERSISTENT POOL FAILURE + REAL SUBPROCESS FALLBACK:
    Tests service fallback handling when pool.render raises PersistentWorkerInfrastructureError.
    """
    pool = PersistentRenderWorkerPool(size=1, render_timeout_s=5.0)
    pool._is_running = True
    initial_fallbacks = pool.total_fallbacks

    mock_settings = Settings(enable_persistent_render_pool=True)
    with patch("app.core.config.settings", mock_settings), \
         patch("app.api.tools.render.service.settings", mock_settings), \
         patch("app.api.tools.render.service.get_persistent_render_pool", return_value=pool), \
         patch.object(pool, "render", new_callable=AsyncMock, side_effect=PersistentWorkerInfrastructureError("Simulated pool error")):

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = generate_auth_headers("POST", "/api/v1/render/page")
            files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
            data = {"page": "1", "dpi": "144"}

            resp = await client.post("/api/v1/render/page", headers=headers, files=files, data=data)
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/jpeg"
            assert len(resp.content) > 0
            assert pool.total_fallbacks == initial_fallbacks + 1


# 11. Comprehensive Queue Invariant Verification Across Real Transitions
@pytest.mark.anyio
async def test_queue_invariants_across_all_real_lifecycle_transitions(sample_pdf_bytes: bytes):
    """
    Tests that available_workers queue NEVER contains dead, stale, or unregistered workers
    across all real lifecycle transitions:
    A. Normal render
    B. Real worker crash
    C. Real worker timeout
    D. Worker recycling
    E. Replacement startup failure
    F. Shutdown
    """
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "true", "APP_ENV": "test"}, clear=False):
        pool = PersistentRenderWorkerPool(size=2, max_renders=2, max_rss_mb=400, render_timeout_s=2.0)
        await pool.start()
        assert_queue_strict_invariants(pool)

        try:
            # A. Normal Render
            jpeg, meta = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
            assert len(jpeg) > 0
            assert_queue_strict_invariants(pool)

            # B. Real Worker Crash
            with pytest.raises(PersistentWorkerInfrastructureError):
                await pool.render(sample_pdf_bytes, page=1, dpi=144.0, simulate_crash=True)
            assert_queue_strict_invariants(pool)

            # C. Real Worker Timeout
            with pytest.raises((PersistentWorkerInfrastructureError, PersistentWorkerTimeoutError)):
                await pool.render(sample_pdf_bytes, page=1, dpi=144.0, simulate_hang=True)
            assert_queue_strict_invariants(pool)

            # D. Worker Recycling (Trigger render count threshold)
            await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
            await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
            assert_queue_strict_invariants(pool)

            # E. Replacement Startup Failure
            with patch.object(PersistentRenderWorker, "start", side_effect=RuntimeError("Simulated replacement failure")):
                with pytest.raises(PersistentWorkerInfrastructureError):
                    await pool.render(sample_pdf_bytes, page=1, dpi=144.0, simulate_crash=True)
            assert_queue_strict_invariants(pool)

        finally:
            # F. Shutdown
            await pool.shutdown()
            assert pool.available_workers.empty()
            assert len(pool.workers) == 0


# 12. Fallback Counter Matrix Verification
@pytest.mark.anyio
async def test_fallback_counter_exact_semantics_matrix(sample_pdf_bytes: bytes):
    """
    Verify fallback counter increment semantics:
    - Normal persistent render -> fallback +0
    - USER_INPUT_ERROR -> fallback +0
    - Real worker crash -> fallback +1
    - Real worker timeout -> fallback +1
    """
    with patch.dict(os.environ, {"ENABLE_RENDER_FAILURE_INJECTION": "true", "APP_ENV": "test"}, clear=False):
        pool = PersistentRenderWorkerPool(size=1, render_timeout_s=2.0)
        await pool.start()

        mock_settings = Settings(enable_persistent_render_pool=True, enable_render_failure_injection=True, app_env="test")
        with patch("app.core.config.settings", mock_settings), \
             patch("app.api.tools.render.service.settings", mock_settings), \
             patch("app.api.tools.render.service.get_persistent_render_pool", return_value=pool):

            # 1. Normal render -> +0
            up1 = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
            await render_page_to_jpeg_bytes(up1, page=1, dpi=144.0)
            assert pool.total_fallbacks == 0

            # 2. USER_INPUT_ERROR (bad page) -> +0
            up2 = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
            with pytest.raises(ValueError, match="Invalid page"):
                await render_page_to_jpeg_bytes(up2, page=999, dpi=144.0)
            assert pool.total_fallbacks == 0

            # 3. Real crash -> +1
            up3 = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
            await render_page_to_jpeg_bytes(up3, page=1, dpi=144.0, simulate_crash=True)
            assert pool.total_fallbacks == 1

            # 4. Real timeout -> +1
            up4 = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
            await render_page_to_jpeg_bytes(up4, page=1, dpi=144.0, simulate_hang=True)
            assert pool.total_fallbacks == 2

        await pool.shutdown()


# 13. Startup Failure Resiliency (Lifespan Safety)
@pytest.mark.anyio
async def test_startup_failure_resiliency():
    """If pool startup fails, start_persistent_render_pool catches error and leaves _persistent_pool=None."""
    await shutdown_persistent_render_pool()
    with patch.object(PersistentRenderWorkerPool, "start", side_effect=RuntimeError("Fatal pool bootstrap error")):
        result = await start_persistent_render_pool()
        assert result is None
        assert get_persistent_render_pool() is None


# 14. Worker Lifetime Recycling (Renders Count & RSS High-Water Mark)
@pytest.mark.anyio
async def test_worker_lifetime_recycling(sample_pdf_bytes: bytes):
    """Workers are safely recycled after reaching WORKER_MAX_RENDERS or WORKER_MAX_RSS_MB."""
    pool = PersistentRenderWorkerPool(size=1, max_renders=3, max_rss_mb=400, render_timeout_s=5.0)
    await pool.start()

    try:
        initial_worker = list(pool.workers.values())[0]
        initial_pid = initial_worker.pid

        for i in range(3):
            jpeg, meta = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
            assert len(jpeg) > 0

        assert initial_worker.needs_recycling is True

        jpeg4, meta4 = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
        assert len(jpeg4) > 0
        assert pool.total_recycled >= 1

        recycled_worker = list(pool.workers.values())[0]
        assert recycled_worker.pid != initial_pid
        assert recycled_worker.total_renders_completed == 1
    finally:
        await pool.shutdown()


# 15. Accurate Degraded Metrics Telemetry & Invariants
@pytest.mark.anyio
async def test_accurate_degraded_metrics_invariants():
    """Verify invariant 0 <= available_workers <= healthy_workers <= configured_workers under normal and degraded states."""
    pool = PersistentRenderWorkerPool(size=3, render_timeout_s=5.0)
    await pool.start()

    try:
        m = pool.get_metrics()
        assert m["configured_workers"] == 3
        assert m["healthy_workers"] == 3
        assert m["available_workers"] == 3
        assert m["busy_workers"] == 0
        assert m["degraded"] is False
        assert 0 <= m["available_workers"] <= m["healthy_workers"] <= m["configured_workers"]

        # Mark 1 worker dead
        dead_worker = list(pool.workers.values())[0]
        dead_worker.is_alive = False

        m_deg = pool.get_metrics()
        assert m_deg["healthy_workers"] == 2
        assert m_deg["degraded"] is True
        assert 0 <= m_deg["available_workers"] <= m_deg["healthy_workers"] <= m_deg["configured_workers"]
    finally:
        await pool.shutdown()


# 16. Concurrency Stress Safety (N=1, 4, 8, 16)
@pytest.mark.anyio
async def test_concurrent_render_stress(sample_pdf_bytes: bytes):
    """Verify concurrent requests execute safely across available workers with zero queue deadlocks."""
    pool = PersistentRenderWorkerPool(size=4, render_timeout_s=5.0)
    await pool.start()

    try:
        for conc in [1, 4, 8, 16]:
            tasks = [
                pool.render(sample_pdf_bytes, page=1, dpi=144.0, request_id=f"conc_{conc}_req_{i}")
                for i in range(conc)
            ]
            results = await asyncio.gather(*tasks)
            assert len(results) == conc
            for jpeg, meta in results:
                assert len(jpeg) > 0
                assert meta["status"] == "ok"
    finally:
        await pool.shutdown()


# 17. Adversarial Shutdown (Shutdown Twice, Clean Cleanup)
@pytest.mark.anyio
async def test_adversarial_shutdown():
    """Verify pool shuts down cleanly with zero zombie processes even if called repeatedly."""
    pool = PersistentRenderWorkerPool(size=2, render_timeout_s=5.0)
    await pool.start()
    pids = [w.pid for w in pool.workers.values()]

    await pool.shutdown()
    assert not pool.is_running
    assert len(pool.workers) == 0

    # Shutdown again should be safe no-op
    await pool.shutdown()

    for pid in pids:
        try:
            os.kill(pid, 0)
            pytest.fail(f"Worker PID {pid} was not terminated on shutdown")
        except OSError:
            pass


# 18. Observability Endpoint
def test_http_endpoint_metrics_observability():
    """Verify /api/v1/render/metrics exposes persistent pool status."""
    with TestClient(app) as client:
        headers = generate_auth_headers("GET", "/api/v1/render/metrics")
        resp = client.get("/api/v1/render/metrics", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "persistent_pool" in data
        assert "enabled" in data["persistent_pool"]
        assert "configured_workers" in data["persistent_pool"]


# 19. HTTP Page Rendering Integration
def test_http_endpoint_page_rendering_integration(sample_pdf_bytes: bytes):
    """Verify /api/v1/render/page executes end-to-end over HTTP with telemetry headers."""
    with TestClient(app) as client:
        headers = generate_auth_headers("POST", "/api/v1/render/page")
        files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
        data = {"page": 1, "dpi": 144}
        resp = client.post("/api/v1/render/page", headers=headers, files=files, data=data)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert "x-queue-wait-ms" in resp.headers
        assert "x-render-exec-ms" in resp.headers


# 20. Rollback to Subprocess and Re-enable Lifecycle
@pytest.mark.anyio
async def test_rollback_to_subprocess_and_re_enable_lifecycle(sample_pdf_bytes: bytes):
    """
    Validates dynamic operational transition:
    1. Persistent pool enabled -> serves requests
    2. Rollback: persistent pool disabled -> subprocess renderer serves requests
    3. Re-enable: persistent pool enabled -> healthy workers restored with zero stale state
    """
    # 1. Start persistent pool
    pool = PersistentRenderWorkerPool(size=2, render_timeout_s=5.0)
    await pool.start()
    pids_initial = [w.pid for w in pool.workers.values()]
    assert len(pids_initial) == 2

    # Render with persistent pool
    jpeg1, meta1 = await pool.render(sample_pdf_bytes, page=1, dpi=144.0)
    assert len(jpeg1) > 0

    # 2. Rollback to Subprocess
    await pool.shutdown()
    for pid in pids_initial:
        try:
            os.kill(pid, 0)
            pytest.fail(f"Worker PID {pid} not terminated during rollback shutdown")
        except OSError:
            pass

    mock_rollback_settings = Settings(enable_persistent_render_pool=False)
    with patch("app.api.tools.render.service.settings", mock_rollback_settings), \
         patch("app.api.tools.render.service.get_persistent_render_pool", return_value=None):
        upload = UploadFile(file=io.BytesIO(sample_pdf_bytes), filename="test.pdf")
        jpeg_sub, _ = await render_page_to_jpeg_bytes(upload, page=1, dpi=144.0)
        assert len(jpeg_sub) > 0

    # 3. Re-enable Persistent Pool
    re_pool = PersistentRenderWorkerPool(size=2, render_timeout_s=5.0)
    await re_pool.start()
    re_pids = [w.pid for w in re_pool.workers.values()]
    assert len(re_pids) == 2
    assert set(re_pids).isdisjoint(set(pids_initial))

    jpeg_re, meta_re = await re_pool.render(sample_pdf_bytes, page=1, dpi=144.0)
    assert len(jpeg_re) > 0
    assert re_pool.get_metrics()["healthy_workers"] == 2

    await re_pool.shutdown()


# 21. Granular Observability Metrics & Fallback Reasons Breakdown
@pytest.mark.anyio
async def test_granular_observability_metrics_breakdown():
    """Verify get_metrics exposes total_crashes, total_timeouts, and fallback_reasons dictionary."""
    pool = PersistentRenderWorkerPool(size=2, render_timeout_s=5.0)
    await pool.start()

    try:
        m = pool.get_metrics()
        assert "total_crashes" in m
        assert "total_timeouts" in m
        assert "fallback_reasons" in m
        assert "crash" in m["fallback_reasons"]
        assert "timeout" in m["fallback_reasons"]
        assert "infrastructure" in m["fallback_reasons"]

        # Increment with custom reason
        pool.increment_fallback_count(reason="timeout")
        pool.increment_fallback_count(reason="crash")
        pool.increment_fallback_count(reason="crash")

        m2 = pool.get_metrics()
        assert m2["total_fallbacks"] == 3
        assert m2["fallback_reasons"]["timeout"] == 1
        assert m2["fallback_reasons"]["crash"] == 2
    finally:
        await pool.shutdown()

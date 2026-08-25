from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# Production-owned persistent worker process script
WORKER_DIR = Path(__file__).resolve().parents[4] # pdfnest-worker directory
PYTHON_BIN = sys.executable
WORKER_SCRIPT = Path(__file__).resolve().parent / "worker_process.py"


class PersistentWorkerError(Exception):
    """Base exception for persistent render worker errors."""
    pass


class PersistentWorkerTimeoutError(PersistentWorkerError):
    """Raised when a persistent worker exceeds execution timeout."""
    pass


class PersistentWorkerInfrastructureError(PersistentWorkerError):
    """
    Raised when an infrastructure failure occurs (crash, pipe error, timeout, unhandled exit).
    Triggers automatic fallback to the certified subprocess renderer.
    """
    pass


class PersistentRenderWorker:
    """
    Manages the lifecycle of a single persistent, pre-warmed render worker process.
    """

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.pid: Optional[int] = None
        self.init_wall_ms: float = 0.0
        self.init_cpu_ms: float = 0.0
        self.is_alive: bool = False
        self.total_renders_completed: int = 0
        self.total_errors: int = 0
        self.last_max_rss_high_water_mb: float = 0.0 # Process high-water mark RSS observed
        self.needs_recycling: bool = False
        self.lock = asyncio.Lock()
        self._stderr_drain_task: Optional[asyncio.Task] = None

    async def _drain_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
        except Exception:
            pass

    async def start(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(WORKER_DIR)

        self.proc = await asyncio.create_subprocess_exec(
            PYTHON_BIN,
            str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self.pid = self.proc.pid
        self._stderr_drain_task = asyncio.create_task(self._drain_stderr())

        # Read ready handshake
        try:
            len_bytes = await asyncio.wait_for(self.proc.stdout.readexactly(4), timeout=10.0)
            h_len = struct.unpack(">I", len_bytes)[0]
            if h_len > 64 * 1024:
                raise PersistentWorkerInfrastructureError(f"Worker {self.worker_id} handshake header too large: {h_len}")
            h_data = await asyncio.wait_for(self.proc.stdout.readexactly(h_len), timeout=10.0)
            handshake = json.loads(h_data.decode("utf-8"))
            if handshake.get("status") != "ready":
                raise PersistentWorkerInfrastructureError(f"Unexpected handshake status: {handshake}")
            self.init_wall_ms = handshake.get("init_wall_ms", 0.0)
            self.init_cpu_ms = handshake.get("init_cpu_ms", 0.0)
            self.is_alive = True
            self.needs_recycling = False
        except Exception as exc:
            await self.kill()
            raise PersistentWorkerInfrastructureError(f"Worker {self.worker_id} failed to initialize: {exc}") from exc

    async def render_page(
        self,
        pdf_bytes: bytes,
        page: Any = 1,
        dpi: Any = 144.0,
        clip_x0: Optional[Any] = None,
        clip_y0: Optional[Any] = None,
        clip_x1: Optional[Any] = None,
        clip_y1: Optional[Any] = None,
        clip_raw: Optional[Any] = None,
        request_id: str = "",
        timeout_s: float = 30.0,
        simulate_crash: bool = False,
        simulate_hang: bool = False,
    ) -> Tuple[bytes, Dict[str, Any]]:
        if not self.is_alive or not self.proc:
            raise PersistentWorkerInfrastructureError(f"Worker {self.worker_id} is not running")

        if clip_raw is not None:
            clip = clip_raw
        elif clip_x0 is not None or clip_y0 is not None or clip_x1 is not None or clip_y1 is not None:
            clip = [clip_x0, clip_y0, clip_x1, clip_y1]
        else:
            clip = None

        header = {
            "request_id": request_id,
            "action": "render",
            "page": page,
            "dpi": dpi,
            "clip": clip,
            "payload_len": len(pdf_bytes),
            "simulate_crash": simulate_crash,
            "simulate_hang": simulate_hang,
        }
        header_bytes = json.dumps(header).encode("utf-8")
        msg = struct.pack(">I", len(header_bytes)) + header_bytes + pdf_bytes

        async with self.lock:
            try:
                # Write request to worker stdin pipe
                self.proc.stdin.write(msg)
                await self.proc.stdin.drain()

                # Read response with strict execution timeout
                async def _read_resp():
                    len_bytes = await self.proc.stdout.readexactly(4)
                    resp_len = struct.unpack(">I", len_bytes)[0]
                    if resp_len > 64 * 1024:
                        raise PersistentWorkerInfrastructureError(f"Worker response header exceeds limit: {resp_len}")
                    resp_header_bytes = await self.proc.stdout.readexactly(resp_len)
                    resp_meta = json.loads(resp_header_bytes.decode("utf-8"))
                    payload_len = resp_meta.get("payload_len", 0)
                    if payload_len > 100 * 1024 * 1024:
                        raise PersistentWorkerInfrastructureError(f"Worker response payload exceeds limit: {payload_len}")
                    payload = await self.proc.stdout.readexactly(payload_len) if payload_len > 0 else b""
                    return payload, resp_meta

                jpeg_bytes, meta = await asyncio.wait_for(_read_resp(), timeout=timeout_s)

                if meta.get("status") != "ok":
                    self.total_errors += 1
                    err_type = meta.get("error_type", "")
                    err_msg = meta.get("error_message", "Unknown render error")
                    if err_type == "USER_INPUT_ERROR":
                        raise ValueError(err_msg)
                    raise PersistentWorkerInfrastructureError(f"Render failed on worker [{err_type}]: {err_msg}")

                self.total_renders_completed += 1
                max_rss_kb = meta.get("max_rss_kb", 0)
                self.last_max_rss_high_water_mb = max_rss_kb / 1024.0

                return jpeg_bytes, meta

            except asyncio.TimeoutError:
                self.is_alive = False
                await self.kill()
                raise PersistentWorkerTimeoutError(f"Worker {self.worker_id} timed out after {timeout_s}s")
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as exc:
                self.is_alive = False
                await self.kill()
                raise PersistentWorkerInfrastructureError(f"Worker {self.worker_id} crashed or disconnected: {exc}") from exc
            except ValueError:
                # Normal user validation error, worker remains alive
                raise
            except Exception as exc:
                self.is_alive = False
                await self.kill()
                raise PersistentWorkerInfrastructureError(f"Worker {self.worker_id} error: {exc}") from exc

    async def kill(self) -> None:
        self.is_alive = False
        if self._stderr_drain_task:
            self._stderr_drain_task.cancel()
            self._stderr_drain_task = None
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except Exception:
                pass
            self.proc = None


class PersistentRenderWorkerPool:
    """
    Manages an async pool of persistent pre-warmed render workers with automated recycling,
    crash recovery, timeout enforcement, queue poisoning protection, and truthful telemetry.
    """

    def __init__(
        self,
        size: int = 4,
        max_renders: int = 1000,
        max_rss_mb: int = 350,
        render_timeout_s: float = 30.0,
    ):
        self.size = size
        self.max_renders = max_renders
        self.max_rss_mb = max_rss_mb
        self.render_timeout_s = render_timeout_s
        self.workers: Dict[int, PersistentRenderWorker] = {}
        self.available_workers: asyncio.Queue[PersistentRenderWorker] = asyncio.Queue()
        self.total_completed: int = 0
        self.total_failed: int = 0
        self.total_restarts: int = 0
        self.total_fallbacks: int = 0
        self.total_recycled: int = 0
        self.total_crashes: int = 0
        self.total_timeouts: int = 0
        self.fallback_reasons: Dict[str, int] = {"crash": 0, "timeout": 0, "infrastructure": 0}
        self._is_running = False
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def start(self) -> None:
        self._is_running = True
        started_workers = []
        for i in range(self.size):
            worker = PersistentRenderWorker(worker_id=i + 1)
            try:
                await worker.start()
                self.workers[worker.worker_id] = worker
                await self.available_workers.put(worker)
                started_workers.append(worker)
            except Exception as exc:
                logger.error(f"Worker {i + 1} failed startup: {exc}")
                for w in started_workers:
                    await w.kill()
                self.workers.clear()
                self._is_running = False
                raise PersistentWorkerInfrastructureError(f"Failed to start PersistentRenderWorkerPool: {exc}") from exc

        logger.info(f"PersistentRenderWorkerPool initialized with {self.size} workers.")

    async def _replace_worker(self, dead_worker: PersistentRenderWorker, is_recycling: bool = False) -> PersistentRenderWorker:
        """Helper to safely kill and spawn a fresh replacement worker."""
        await dead_worker.kill()
        self.total_restarts += 1
        if is_recycling:
            self.total_recycled += 1
        new_worker = PersistentRenderWorker(worker_id=dead_worker.worker_id)
        await new_worker.start()
        self.workers[new_worker.worker_id] = new_worker
        return new_worker

    async def render(
        self,
        pdf_bytes: bytes,
        page: Any = 1,
        dpi: Any = 144.0,
        clip_x0: Optional[Any] = None,
        clip_y0: Optional[Any] = None,
        clip_x1: Optional[Any] = None,
        clip_y1: Optional[Any] = None,
        clip_raw: Optional[Any] = None,
        request_id: str = "",
        simulate_crash: bool = False,
        simulate_hang: bool = False,
    ) -> Tuple[bytes, Dict[str, Any]]:
        if not self._is_running:
            raise PersistentWorkerInfrastructureError("PersistentRenderWorkerPool is not running")

        t_acquire_start = time.perf_counter()
        worker = await self.available_workers.get()
        queue_wait_ms = (time.perf_counter() - t_acquire_start) * 1000.0

        should_return_to_queue = False

        try:
            # Check if worker needs recycling or was terminated
            if not worker.is_alive or worker.needs_recycling:
                is_rec = worker.needs_recycling
                try:
                    worker = await self._replace_worker(worker, is_recycling=is_rec)
                except Exception as repl_exc:
                    logger.error(f"Worker replacement failed: {repl_exc}")
                    # Invariant: Never put broken worker back into queue
                    raise PersistentWorkerInfrastructureError(f"Worker replacement failed: {repl_exc}") from repl_exc

            jpeg_bytes, meta = await worker.render_page(
                pdf_bytes=pdf_bytes,
                page=page,
                dpi=dpi,
                clip_x0=clip_x0,
                clip_y0=clip_y0,
                clip_x1=clip_x1,
                clip_y1=clip_y1,
                clip_raw=clip_raw,
                request_id=request_id,
                timeout_s=self.render_timeout_s,
                simulate_crash=simulate_crash,
                simulate_hang=simulate_hang,
            )
            meta["queue_wait_ms"] = round(queue_wait_ms, 2)
            self.total_completed += 1

            # Check if worker reached lifetime render count or high-water RSS threshold
            if worker.total_renders_completed >= self.max_renders or worker.last_max_rss_high_water_mb >= self.max_rss_mb:
                worker.needs_recycling = True

            should_return_to_queue = True
            return jpeg_bytes, meta

        except ValueError:
            # User/input error: worker remains healthy and should return to queue
            self.total_failed += 1
            should_return_to_queue = worker.is_alive
            raise

        except PersistentWorkerTimeoutError as exc:
            self.total_failed += 1
            self.total_timeouts += 1
            # Attempt to replace timed-out worker immediately
            try:
                worker = await self._replace_worker(worker, is_recycling=False)
                should_return_to_queue = True
            except Exception as replace_exc:
                logger.error(f"Failed to replace worker {worker.worker_id} after timeout: {replace_exc}")
                should_return_to_queue = False
            raise

        except (PersistentWorkerInfrastructureError, PersistentWorkerError) as exc:
            self.total_failed += 1
            self.total_crashes += 1
            # Attempt to replace dead worker immediately
            try:
                worker = await self._replace_worker(worker, is_recycling=False)
                should_return_to_queue = True
            except Exception as replace_exc:
                logger.error(f"Failed to replace worker {worker.worker_id} after failure: {replace_exc}")
                should_return_to_queue = False
            raise PersistentWorkerInfrastructureError(str(exc)) from exc

        finally:
            # Invariant: Only return alive, valid workers to the available queue
            if self._is_running and should_return_to_queue and worker is not None and worker.is_alive:
                await self.available_workers.put(worker)

    def increment_fallback_count(self, reason: str = "infrastructure") -> None:
        self.total_fallbacks += 1
        if reason in self.fallback_reasons:
            self.fallback_reasons[reason] += 1
        else:
            self.fallback_reasons["infrastructure"] += 1

    def get_metrics(self) -> Dict[str, Any]:
        """
        Returns truthful operational metrics reflecting active, healthy, and degraded state.
        Guarantees invariant: 0 <= available_workers <= healthy_workers <= configured_workers.
        """
        healthy = sum(1 for w in self.workers.values() if w.is_alive)
        avail = min(healthy, self.available_workers.qsize())
        busy = max(0, healthy - avail)
        degraded = healthy < self.size

        return {
            "enabled": True,
            "configured_workers": self.size,
            "healthy_workers": healthy,
            "available_workers": avail,
            "busy_workers": busy,
            "degraded": degraded,
            "max_renders": self.max_renders,
            "max_rss_high_water_mb": self.max_rss_mb,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_crashes": self.total_crashes,
            "total_timeouts": self.total_timeouts,
            "total_restarts": self.total_restarts,
            "total_recycled": self.total_recycled,
            "total_fallbacks": self.total_fallbacks,
            "fallback_reasons": dict(self.fallback_reasons),
        }

    async def shutdown(self) -> None:
        self._is_running = False
        tasks = [w.kill() for w in list(self.workers.values())]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        # Empty available queue
        while not self.available_workers.empty():
            try:
                self.available_workers.get_nowait()
            except Exception:
                break
        logger.info("PersistentRenderWorkerPool shut down cleanly.")


# Global Singleton Pool Instance
_persistent_pool: Optional[PersistentRenderWorkerPool] = None


def get_persistent_render_pool() -> Optional[PersistentRenderWorkerPool]:
    return _persistent_pool


async def start_persistent_render_pool() -> Optional[PersistentRenderWorkerPool]:
    """
    Safely initializes the persistent render pool during application startup.
    If initialization fails, logs a critical warning and leaves _persistent_pool=None
    so the application starts normally and falls back to the certified subprocess renderer.
    """
    global _persistent_pool
    if _persistent_pool is None:
        pool_size = max(1, settings.persistent_render_pool_size)
        try:
            pool = PersistentRenderWorkerPool(
                size=pool_size,
                max_renders=settings.worker_max_renders,
                max_rss_mb=settings.worker_max_rss_mb,
                render_timeout_s=float(os.getenv("RENDER_TIMEOUT_SECONDS", "30.0")),
            )
            await pool.start()
            _persistent_pool = pool
            logger.info(f"Persistent render pool successfully initialized with {pool_size} workers.")
        except Exception as exc:
            logger.error(
                f"CRITICAL: Failed to initialize PersistentRenderWorkerPool: {exc}. "
                "Application will continue running with certified subprocess renderer fallback."
            )
            _persistent_pool = None
    return _persistent_pool


async def shutdown_persistent_render_pool() -> None:
    global _persistent_pool
    if _persistent_pool is not None:
        await _persistent_pool.shutdown()
        _persistent_pool = None

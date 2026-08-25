from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Awaitable, Dict, TypeVar

from fastapi import HTTPException

T = TypeVar("T")


def get_effective_memory_limit_bytes() -> int:
    """
    Reads the container/process memory limit from cgroup v2 or v1 if available,
    falling back to total system physical RAM.
    """
    # 1. Check cgroup v2: /sys/fs/cgroup/memory.max
    cgroup_v2_path = "/sys/fs/cgroup/memory.max"
    if os.path.exists(cgroup_v2_path):
        try:
            with open(cgroup_v2_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content and content != "max":
                    val = int(content)
                    if val > 0:
                        return val
        except Exception:
            pass

    # 2. Check cgroup v1: /sys/fs/cgroup/memory/memory.limit_in_bytes or /sys/fs/cgroup/memory.limit_in_bytes
    for v1_path in ["/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory.limit_in_bytes"]:
        if os.path.exists(v1_path):
            try:
                with open(v1_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        val = int(content)
                        # v1 sets huge number (e.g. >= 2**60) for unlimited
                        if 0 < val < (1 << 60):
                            return val
            except Exception:
                pass

    # 3. Fallback to physical host RAM
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        if page_size > 0 and phys_pages > 0:
            return page_size * phys_pages
    except Exception:
        pass

    # Default fallback: 2 GB
    return 2 * 1024 * 1024 * 1024


def get_default_concurrency_limit() -> int:
    """
    Computes safe default concurrency limit inspected from CPU cores and system/container RAM.
    Can be explicitly overridden via the RENDER_CONCURRENCY_LIMIT environment variable.
    """
    env_val = os.getenv("RENDER_CONCURRENCY_LIMIT")
    if env_val:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass

    cpu_count = os.cpu_count() or 2
    mem_bytes = get_effective_memory_limit_bytes()
    mem_mb = mem_bytes / (1024 * 1024)
    # Allocate up to 512 MB per concurrent scale-8 raster slot
    ram_concurrency = max(1, int(mem_mb // 512))

    # Authoritative calculation: bounded by CPU and RAM, minimum 1, maximum 8
    return min(max(1, cpu_count), max(1, ram_concurrency), 8)


class RenderConcurrencyLimiter:
    """
    In-process concurrency limiter for direct PDF rendering endpoints.
    Protects worker process CPU and RAM from uncontrolled concurrent rasterizations.
    
    NOTE: This is a process-level limiter (asyncio.Semaphore). Total node capacity is:
          Effective Concurrency = Worker Processes * max_concurrency
    """

    def __init__(
        self,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
        render_timeout: float | None = None,
        queue_timeout: float | None = None,
    ):
        self.max_concurrency = max_concurrency or get_default_concurrency_limit()
        self.max_queue = (
            max_queue
            if max_queue is not None
            else int(os.getenv("RENDER_QUEUE_LIMIT", "16"))
        )
        self.render_timeout = (
            render_timeout
            if render_timeout is not None
            else float(os.getenv("RENDER_TIMEOUT_SECONDS", "30.0"))
        )
        self.queue_timeout = (
            queue_timeout
            if queue_timeout is not None
            else float(os.getenv("RENDER_QUEUE_TIMEOUT_SECONDS", "10.0"))
        )

        self._semaphore: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()

        # Telemetry metrics
        self.active_renders = 0
        self.peak_active_renders = 0
        self.queued_renders = 0
        self.peak_queued_renders = 0
        self.total_renders_completed = 0
        self.total_renders_rejected = 0
        self.total_renders_timed_out = 0
        self.total_renders_failed = 0
        self.total_duration_ms = 0.0

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, None]:
        """
        Acquires a concurrency slot within bounded queue limits.
        Ensures cancellation-safe and exception-safe slot release via finally.
        """
        sem = self._get_semaphore()

        # 1. Queue Bounded Check
        async with self._lock:
            if self.queued_renders >= self.max_queue:
                self.total_renders_rejected += 1
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "RENDER_QUEUE_FULL",
                        "message": "Render capacity saturated. Please retry shortly.",
                        "active": self.active_renders,
                        "queued": self.queued_renders,
                        "limit": self.max_concurrency,
                    },
                    headers={"Retry-After": "2"},
                )
            self.queued_renders += 1
            if self.queued_renders > self.peak_queued_renders:
                self.peak_queued_renders = self.queued_renders

        # 2. Wait for Concurrency Slot with Timeout
        acquired = False
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.queue_timeout)
            acquired = True
        except asyncio.TimeoutError as exc:
            async with self._lock:
                self.total_renders_rejected += 1
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RENDER_QUEUE_TIMEOUT",
                    "message": f"Render queue wait exceeded {self.queue_timeout:.1f}s.",
                },
                headers={"Retry-After": "3"},
            ) from exc
        finally:
            async with self._lock:
                self.queued_renders -= 1
                if acquired:
                    self.active_renders += 1
                    if self.active_renders > self.peak_active_renders:
                        self.peak_active_renders = self.active_renders

        # 3. Execution Guard with Cancellation & Exception Safe Slot Release
        start_time = time.perf_counter()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            sem.release()
            async with self._lock:
                self.active_renders -= 1
                if failed:
                    self.total_renders_failed += 1
                else:
                    self.total_renders_completed += 1
                    self.total_duration_ms += elapsed_ms

    async def run(self, coro: Awaitable[T]) -> T:
        """
        Executes a render coroutine under the concurrency limiter with strict execution timeout.
        """
        async with self.acquire():
            try:
                return await asyncio.wait_for(coro, timeout=self.render_timeout)
            except asyncio.TimeoutError as exc:
                async with self._lock:
                    self.total_renders_timed_out += 1
                raise HTTPException(
                    status_code=504,
                    detail={
                        "code": "RENDER_TIMEOUT",
                        "message": f"Render processing exceeded {self.render_timeout:.1f}s limit",
                    },
                ) from exc

    def get_metrics(self) -> Dict[str, Any]:
        """
        Returns telemetry metrics. avg_duration_ms is the cumulative average duration.
        """
        avg_ms = 0.0
        if self.total_renders_completed > 0:
            avg_ms = self.total_duration_ms / self.total_renders_completed

        return {
            "max_concurrency": self.max_concurrency,
            "max_queue": self.max_queue,
            "active_renders": self.active_renders,
            "peak_active_renders": self.peak_active_renders,
            "queued_renders": self.queued_renders,
            "peak_queued_renders": self.peak_queued_renders,
            "total_completed": self.total_renders_completed,
            "total_rejected": self.total_renders_rejected,
            "total_timed_out": self.total_renders_timed_out,
            "total_failed": self.total_renders_failed,
            "avg_duration_ms": round(avg_ms, 2),
        }

    def reset_metrics(self) -> None:
        self.peak_active_renders = self.active_renders
        self.peak_queued_renders = self.queued_renders
        self.total_renders_completed = 0
        self.total_renders_rejected = 0
        self.total_renders_timed_out = 0
        self.total_renders_failed = 0
        self.total_duration_ms = 0.0


# Global Singleton Instance for Worker
render_limiter = RenderConcurrencyLimiter()

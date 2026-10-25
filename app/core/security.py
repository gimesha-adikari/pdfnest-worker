from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import re
import tempfile
import threading
import time
from typing import BinaryIO


from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

# Explicit public routes that MUST remain accessible without Worker authentication
PUBLIC_PATHS = {
    "/",
    "/health",
    "/health/live",
    "/health/ready",
    "/home",
    "/docs",
    "/openapi.json",
    "/redoc",
}

_SEEN_NONCES: dict[str, float] = {}


def clean_expired_nonces(now: float, ttl: float = 300.0) -> None:
    expired = [nonce for nonce, ts in _SEEN_NONCES.items() if now - ts > ttl]
    for nonce in expired:
        _SEEN_NONCES.pop(nonce, None)


def is_public_path(path: str) -> bool:
    normalized = path.rstrip("/")
    if not normalized:
        normalized = "/"
    if normalized in PUBLIC_PATHS:
        return True
    if normalized.startswith("/health"):
        return True
    return False


# 100 MiB file budget plus bounded multipart/generated-document headroom.
MAX_AUTH_BODY_BYTES = 128 * 1024 * 1024
AUTH_BODY_CHUNK_BYTES = 64 * 1024
AUTH_BODY_TIMEOUT_SECONDS = 300


class _BodyRejected(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


async def _file_io(function, *args):
    # Uvicorn may cancel the asyncio Task directly. Wait for the bounded file
    # operation before closing its descriptor, even under repeated cancellation.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise


class WorkerAuthMiddleware:
    """Authenticate headers, verify staged raw bytes, then dispatch exactly once.

    A pure ASGI receive wrapper avoids BaseHTTPMiddleware's request-body cache.
    No protected route can run before verification. Four private spools cap this
    middleware's disk data at 512 MiB per API process (not a host-wide quota).
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_AUTH_BODY_BYTES,
                 body_timeout: float = AUTH_BODY_TIMEOUT_SECONDS, temp_dir: str | None = None):
        if not 0 < max_body_bytes <= MAX_AUTH_BODY_BYTES or not 0 < body_timeout <= AUTH_BODY_TIMEOUT_SECONDS:
            raise ValueError("invalid worker authentication body limits")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_timeout = body_timeout
        self.temp_dir = temp_dir
        self._slots = threading.BoundedSemaphore(4)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_public_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        spool: BinaryIO | None = None
        acquired = False
        dispatched = False

        def close_spool() -> None:
            nonlocal spool, acquired
            file, spool = spool, None
            try:
                if file is not None:
                    file.close()
            finally:
                if acquired:
                    self._slots.release()
                    acquired = False

        try:
            signature = request.headers.get("X-Worker-Signature", "")
            timestamp_str = request.headers.get("X-Worker-Timestamp", "")
            nonce = request.headers.get("X-Worker-Nonce", "")
            body_hash = request.headers.get("X-Worker-Body-Hash", "")
            if not signature or not timestamp_str or not nonce:
                raise _BodyRejected(401, "Missing worker authentication signature headers")
            auth_names = {b"x-worker-signature", b"x-worker-timestamp", b"x-worker-nonce", b"x-worker-body-hash"}
            for name in auth_names:
                if sum(key.lower() == name for key, _ in scope["headers"]) > 1:
                    raise _BodyRejected(401, "Ambiguous worker authentication headers")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
                raise _BodyRejected(401, "Invalid worker authentication signature")
            if not re.fullmatch(r"[0-9a-f]{64}", body_hash):
                raise _BodyRejected(401, "Missing or malformed worker body digest")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nonce):
                raise _BodyRejected(401, "Invalid worker authentication nonce")
            try:
                req_timestamp = float(timestamp_str)
                skew = float(os.getenv("WORKER_CLOCK_SKEW_WINDOW", "300"))
            except ValueError:
                raise _BodyRejected(401, "Invalid worker authentication timestamp format") from None
            if not math.isfinite(skew) or skew <= 0:
                raise _BodyRejected(503, "Worker authentication configuration unavailable")
            if not math.isfinite(req_timestamp) or abs(time.time() - req_timestamp) > skew:
                raise _BodyRejected(401, "Worker authentication signature timestamp expired or out of bounds")
            secret = os.getenv("WORKER_SHARED_SECRET", settings.worker_shared_secret).strip()
            if not secret:
                secret = "dev-secret-change-in-production"
            target = request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            canonical = f"{request.method.upper()}\n{target}\n{timestamp_str}\n{nonce}\n{body_hash}"
            expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature.lower()):
                raise _BodyRejected(401, "Invalid worker authentication signature")

            declared = request.headers.get("content-length")
            if declared is not None:
                if not re.fullmatch(r"[0-9]+", declared):
                    raise _BodyRejected(400, "Invalid worker body length")
                if int(declared) > self.max_body_bytes:
                    raise _BodyRejected(413, "Worker request body too large")
            digest = hashlib.sha256()
            size = 0
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        raise _BodyRejected(400, "Incomplete worker request body")
                    body = message.get("body", b"")
                    size += len(body)
                    if size > self.max_body_bytes:
                        raise _BodyRejected(413, "Worker request body too large")
                    if body:
                        if spool is None:
                            if not self._slots.acquire(blocking=False):
                                raise _BodyRejected(503, "Worker body staging capacity unavailable")
                            acquired = True
                            spool = tempfile.TemporaryFile(mode="w+b", dir=self.temp_dir)  # private 0600, unlinked
                        for offset in range(0, len(body), AUTH_BODY_CHUNK_BYTES):
                            chunk = memoryview(body)[offset:offset + AUTH_BODY_CHUNK_BYTES]
                            digest.update(chunk)
                            if await _file_io(spool.write, chunk) != len(chunk):
                                raise OSError("short body spool write")
                    else:
                        await asyncio.sleep(0)
                    if not message.get("more_body", False):
                        break
                if declared is not None and size != int(declared):
                    raise _BodyRejected(400, "Incomplete worker request body")
                if not hmac.compare_digest(digest.hexdigest(), body_hash):
                    raise _BodyRejected(401, "Worker request body digest mismatch")
                now = time.time()
                if abs(now - req_timestamp) > skew:
                    raise _BodyRejected(401, "Worker authentication signature timestamp expired or out of bounds")
                # Neither an invalid HMAC nor invalid bytes may poison a nonce.
                await self._reserve_nonce(nonce, now, skew)
                if spool is not None:
                    await _file_io(spool.seek, 0)

            remaining = size

            async def verified_receive():
                nonlocal remaining
                if remaining:
                    data = await _file_io(spool.read, min(remaining, AUTH_BODY_CHUNK_BYTES))
                    if not data:
                        raise OSError("verified body spool ended early")
                    remaining -= len(data)
                    if remaining == 0:
                        close_spool()
                    return {"type": "http.request", "body": data, "more_body": bool(remaining)}
                # Deliver an empty body once, then retain real disconnect semantics.
                nonlocal delivered_empty
                if not delivered_empty:
                    delivered_empty = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return await receive()

            delivered_empty = size > 0
            dispatched = True
            await self.app(scope, verified_receive, send)
        except _BodyRejected as error:
            await JSONResponse(status_code=error.status, content={"detail": error.detail})(scope, receive, send)
        except TimeoutError:
            if dispatched:
                raise
            await JSONResponse(status_code=408, content={"detail": "Worker body verification timed out"})(scope, receive, send)
        except OSError:
            if dispatched:
                raise
            await JSONResponse(status_code=503, content={"detail": "Worker body staging unavailable"})(scope, receive, send)
        finally:
            close_spool()

    @staticmethod
    async def _reserve_nonce(nonce: str, now: float, skew: float) -> None:
        clean_expired_nonces(now, skew)
        redis_url = os.getenv("REDIS_URL")
        from app.core.config import is_managed_environment
        if redis_url:
            try:
                from redis.asyncio import Redis
                async with Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1) as client:
                    reserved = await client.set(f"worker:nonce:{nonce}", "1", ex=max(1, math.ceil(2 * skew)), nx=True)
                if not reserved:
                    raise _BodyRejected(401, "Worker authentication nonce replayed")
                return
            except _BodyRejected:
                raise
            except Exception:
                if is_managed_environment():
                    raise _BodyRejected(503, "Worker replay protection unavailable") from None
        elif is_managed_environment():
            raise _BodyRejected(503, "Worker replay protection unavailable")
        if nonce in _SEEN_NONCES:
            raise _BodyRejected(401, "Worker authentication nonce replayed")
        _SEEN_NONCES[nonce] = now + skew

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

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


class WorkerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if is_public_path(path):
            return await call_next(request)

        secret = os.getenv("WORKER_SHARED_SECRET", settings.worker_shared_secret).strip()
        if not secret:
            secret = "dev-secret-change-in-production"

        signature = request.headers.get("X-Worker-Signature")
        timestamp_str = request.headers.get("X-Worker-Timestamp")
        nonce = request.headers.get("X-Worker-Nonce")

        if not signature or not timestamp_str or not nonce:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing worker authentication signature headers"},
            )

        try:
            req_timestamp = float(timestamp_str)
        except ValueError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid worker authentication timestamp format"},
            )

        now = time.time()
        clock_skew_window = float(os.getenv("WORKER_CLOCK_SKEW_WINDOW", "300"))
        if abs(now - req_timestamp) > clock_skew_window:
            return JSONResponse(
                status_code=401,
                content={"detail": "Worker authentication signature timestamp expired or out of bounds"},
            )

        # Replay protection check
        clean_expired_nonces(now, clock_skew_window)
        redis_url = os.getenv("REDIS_URL")
        nonce_replayed = False
        if redis_url:
            try:
                import redis

                r = redis.Redis.from_url(redis_url, socket_connect_timeout=1)
                key = f"worker:nonce:{nonce}"
                if not r.set(key, "1", ex=int(clock_skew_window), nx=True):
                    nonce_replayed = True
            except Exception:
                if nonce in _SEEN_NONCES:
                    nonce_replayed = True
                else:
                    _SEEN_NONCES[nonce] = now
        else:
            if nonce in _SEEN_NONCES:
                nonce_replayed = True
            else:
                _SEEN_NONCES[nonce] = now

        if nonce_replayed:
            return JSONResponse(
                status_code=401,
                content={"detail": "Worker authentication nonce replayed"},
            )

        # Reconstruct String to Sign
        method = request.method.upper()
        full_path = request.url.path
        if request.url.query:
            full_path += f"?{request.url.query}"

        body_hash = request.headers.get("X-Worker-Body-Hash")
        if body_hash:
            string_to_sign = f"{method}\n{full_path}\n{timestamp_str}\n{nonce}\n{body_hash}"
        else:
            string_to_sign = f"{method}\n{full_path}\n{timestamp_str}\n{nonce}"

        expected_sig = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_sig.lower(), signature.lower()):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid worker authentication signature"},
            )

        return await call_next(request)

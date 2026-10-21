"""Existing HMAC wire contract with isolated credentials and mandatory replay storage."""
from dataclasses import dataclass, field
import hashlib
import hmac
import math
import re
import time
from typing import Callable, Protocol, Sequence

from .errors import AuthenticationError, ProtocolError, ReplayUnavailable
from .target import RequestTarget

Headers = Sequence[tuple[str, str]]


def validated_headers(headers: Headers) -> dict[str, str]:
    """Fail on duplicates: this foundation does not resolve ambiguous header lists."""
    if len(headers) > 100:
        raise ProtocolError()
    result = {}
    size = 0
    for key, value in headers:
        size += len(key) + len(value)
        name = key.lower()
        if (size > 32768 or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
                or name in result or any(ord(c) < 32 or ord(c) > 255 or ord(c) == 127 for c in value)):
            raise ProtocolError()
        result[name] = value
    return result


def _secret(value: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value or any(ord(c) < 33 for c in value):
        raise ProtocolError()


@dataclass(frozen=True)
class Credentials:
    client_secret: str = field(repr=False)
    origin_secret: str = field(repr=False)

    def __post_init__(self):
        _secret(self.client_secret)
        _secret(self.origin_secret)
        if hmac.compare_digest(self.client_secret.encode(), self.origin_secret.encode()):
            raise ProtocolError()


class ReplayStore(Protocol):
    async def reserve(self, nonce: str, *, now: float, expires_at: float) -> bool:
        """Atomic set-if-absent through expires_at; failure must raise, never fallback.

        Must be shared across all authenticators for this client credential.
        No production adapter or in-memory fallback is provided here.
        """
        ...


def sign_headers(target: RequestTarget, secret: str, *, timestamp: str, nonce: str,
                 body_hash: str = '') -> tuple[tuple[str, str], ...]:
    _secret(secret)
    if (not re.fullmatch(r'[0-9]{1,16}(?:\.[0-9]{1,9})?', timestamp)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', nonce)
            or len(body_hash) > 256):
        raise ProtocolError()
    validated_headers([('x-worker-body-hash', body_hash)])
    digest = hmac.new(secret.encode(), target.canonical(timestamp, nonce, body_hash), hashlib.sha256).hexdigest()
    result = [('x-worker-signature', digest), ('x-worker-timestamp', timestamp), ('x-worker-nonce', nonce)]
    if body_hash:
        result.append(('x-worker-body-hash', body_hash))
    return tuple(result)


class ClientAuthenticator:
    def __init__(self, credentials: Credentials, replay: ReplayStore, *,
                 clock: Callable[[], float] = time.time, skew_seconds: float = 300):
        if (isinstance(skew_seconds, bool) or not math.isfinite(skew_seconds)
                or not 0 < skew_seconds <= 300):
            raise ProtocolError()
        self.credentials = credentials
        self.replay = replay
        self.clock = clock
        self.skew_seconds = skew_seconds

    async def authenticate(self, target: RequestTarget, headers: Headers) -> None:
        values = validated_headers(headers)
        timestamp = values.get('x-worker-timestamp', '')
        nonce = values.get('x-worker-nonce', '')
        signature = values.get('x-worker-signature', '')
        now = self.clock()
        if (not re.fullmatch(r'[0-9]{1,16}(?:\.[0-9]{1,9})?', timestamp)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', nonce)
                or not re.fullmatch(r'[0-9a-fA-F]{64}', signature)
                or not math.isfinite(now)
                or abs(now - float(timestamp)) > self.skew_seconds):
            raise AuthenticationError()
        body_hash = values.get('x-worker-body-hash', '')
        expected = dict(sign_headers(target, self.credentials.client_secret,
                                     timestamp=timestamp, nonce=nonce, body_hash=body_hash))['x-worker-signature']
        if not hmac.compare_digest(signature.lower(), expected):
            raise AuthenticationError()
        try:
            # Future timestamps can remain valid for twice the skew window.
            reserved = await self.replay.reserve(nonce, now=now, expires_at=now + 2*self.skew_seconds + 1)
        except Exception:
            raise ReplayUnavailable() from None
        if reserved is not True:
            raise AuthenticationError()

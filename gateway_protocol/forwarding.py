"""Transport-independent, one-attempt streaming contract. No HTTP client or listener.

The adapter must disable redirects/retries, preserve raw targets, yield <=64 KiB
chunks and close transport resources on every exit (including pre-header failure).
No such production adapter is provided. This module cannot open a socket.
"""
import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import AsyncContextManager, AsyncIterator, Callable, Protocol
from urllib.parse import urlsplit
import uuid

from .auth import ClientAuthenticator, Headers, sign_headers, validated_headers
from .errors import ProtocolError
from .routes import require_admission
from .target import RequestTarget


class AmbiguousUpstreamError(ProtocolError):
    upstream_may_have_executed = True


class ForwardingCancelled(asyncio.CancelledError):
    upstream_may_have_executed = True

    def __init__(self):
        super().__init__('Gateway forwarding cancelled; upstream outcome unknown')


@dataclass(frozen=True)
class FixedOrigin:
    url: str

    def __post_init__(self):
        try:
            parsed = urlsplit(self.url)
            valid = (parsed.scheme in {'https', 'http'} and parsed.hostname
                     and not parsed.username and not parsed.password
                     and parsed.path == '' and not parsed.query and not parsed.fragment
                     and re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
                     and (parsed.port is None or 1 <= parsed.port <= 65535)
                     and not any(ord(c) < 33 or ord(c) > 126 for c in self.url)
                     and not any(c in self.url for c in ('?', '#', '@', '\\')))
        except ValueError:
            valid = False
        if not valid:
            raise ProtocolError()


class ByteStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True, repr=False)
class IncomingRequest:
    target: RequestTarget
    headers: Headers = field(repr=False)
    body: ByteStream = field(repr=False)


@dataclass(frozen=True, repr=False)
class UpstreamRequest:
    origin: FixedOrigin
    target: RequestTarget
    headers: Headers = field(repr=False)
    body: AsyncIterator[bytes] = field(repr=False)


@dataclass(frozen=True, repr=False)
class UpstreamResponse:
    status: int
    headers: Headers = field(repr=False)
    body: ByteStream = field(repr=False)


class OneAttemptTransport(Protocol):
    def open_once(self, request: UpstreamRequest) -> AsyncContextManager[UpstreamResponse]:
        """No retries, no redirects, no URL normalization, no ambient credentials.

        Owns and closes socket/session resources on all exit paths. Implementations
        must not log bodies, headers or sensitive targets, nor prebuffer streams.
        """
        ...


class ResponseSink(Protocol):
    async def start(self, status: int, headers: Headers) -> None: ...
    async def write(self, chunk: bytes) -> None:
        """Await backpressure; disconnect must raise. Do not buffer whole responses."""
        ...


_REQUEST_HEADERS = frozenset({
    'content-type', 'accept', 'accept-encoding', 'x-request-id', 'x-owner-identity',
})
_RESPONSE_HEADERS = frozenset({
    'content-type', 'content-encoding', 'content-disposition', 'cache-control', 'etag', 'last-modified',
    'x-queue-wait-ms', 'x-render-exec-ms',
})
_HOP_HEADERS = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
})


def _forward_headers(headers: Headers, allow: frozenset[str], secrets: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    values = validated_headers(headers)
    nominated = {part.strip().lower() for part in values.get('connection', '').split(',')}
    return tuple((key, value) for key, value in values.items()
                 if key in allow and key not in nominated and key not in _HOP_HEADERS
                 and not any(secret in value for secret in secrets))


async def _chunks(stream: ByteStream) -> AsyncIterator[bytes]:
    async for chunk in stream:
        if not isinstance(chunk, bytes) or len(chunk) > 65536:
            raise ProtocolError()
        if chunk:
            yield chunk


class Forwarder:
    def __init__(self, origin: FixedOrigin, authenticator: ClientAuthenticator, *,
                 clock: Callable[[], float] = time.time,
                 nonce_factory: Callable[[], str] = lambda: uuid.uuid4().hex):
        self.origin = origin
        self.authenticator = authenticator
        self.clock = clock
        self.nonce_factory = nonce_factory

    async def forward(self, request: IncomingRequest, transport: OneAttemptTransport,
                      sink: ResponseSink, *, admission_open: bool) -> None:
        # This boolean is ONLY a protocol-test input. It is not a production
        # gate, durable registration, active-execution count or drain guarantee.
        attempted = False
        try:
            await self.authenticator.authenticate(request.target, request.headers)
            require_admission(request.target, admission_open=admission_open)
            credentials = self.authenticator.credentials
            secrets = (credentials.client_secret, credentials.origin_secret)
            outgoing_headers = _forward_headers(request.headers, _REQUEST_HEADERS, secrets)
            body_hash = validated_headers(request.headers).get('x-worker-body-hash', '')
            outgoing_headers += sign_headers(request.target, credentials.origin_secret,
                                             timestamp=str(int(self.clock())), nonce=self.nonce_factory(),
                                             body_hash=body_hash)
            outgoing = UpstreamRequest(self.origin, request.target, outgoing_headers, _chunks(request.body))
            # Even failure before receiving headers is ambiguous: the upstream
            # may have committed/enqueued work. Never automatically resubmit.
            attempted = True
            async with transport.open_once(outgoing) as response:
                try:
                    if (not isinstance(response.status, int) or isinstance(response.status, bool)
                            or not 200 <= response.status <= 599 or 300 <= response.status <= 399):
                        raise ProtocolError()
                    response_headers = _forward_headers(response.headers, _RESPONSE_HEADERS, secrets)
                    await sink.start(response.status, response_headers)
                    async for chunk in _chunks(response.body):
                        await sink.write(chunk)
                finally:
                    await response.body.aclose()
        except asyncio.CancelledError:
            if attempted:
                raise ForwardingCancelled() from None
            raise
        except ProtocolError:
            if attempted:
                raise AmbiguousUpstreamError() from None
            raise
        except Exception:
            if attempted:
                raise AmbiguousUpstreamError() from None
            raise ProtocolError() from None
        finally:
            try:
                await request.body.aclose()
            except asyncio.CancelledError:
                if attempted:
                    raise ForwardingCancelled() from None
                raise
            except Exception:
                if attempted:
                    raise AmbiguousUpstreamError() from None
                raise ProtocolError() from None

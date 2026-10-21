"""Fake transport exercises flow/backpressure; no HTTP listener or socket is built."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import tracemalloc

import pytest

from gateway_protocol.auth import Credentials, ClientAuthenticator, sign_headers
from gateway_protocol.errors import ProtocolError
from gateway_protocol.forwarding import (
    FixedOrigin, Forwarder, IncomingRequest, UpstreamResponse,
    AmbiguousUpstreamError, ForwardingCancelled,
)
from gateway_protocol.target import RequestTarget
from test_gateway_protocol import CLIENT, ORIGIN, NOW, MemoryReplay


class Body:
    def __init__(self, chunks=()):
        self.chunks = iter(chunks)
        self.closed = False
        self.read = 0

    def __aiter__(self): return self

    async def __anext__(self):
        try: value = next(self.chunks)
        except StopIteration: raise StopAsyncIteration
        self.read += 1
        return value

    async def aclose(self): self.closed = True


class FakeTransport:
    def __init__(self, response=None, fail=None):
        self.response = response or UpstreamResponse(200, (('content-type', 'application/pdf'),), Body([b'pdf']))
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.request = None
        self.digest = hashlib.sha256()

    @asynccontextmanager
    async def open_once(self, request):
        self.calls += 1
        self.request = request
        try:
            async for chunk in request.body:
                self.digest.update(chunk)
            if self.fail: raise self.fail
            yield self.response
        finally:
            self.closed = True
            await self.response.body.aclose()


class Sink:
    def __init__(self):
        self.head = None
        self.digest = hashlib.sha256()
        self.count = 0

    async def start(self, status, headers): self.head = (status, headers)

    async def write(self, chunk):
        self.digest.update(chunk)
        self.count += len(chunk)


def make_forwarder():
    credentials = Credentials(CLIENT, ORIGIN)
    auth = ClientAuthenticator(credentials, MemoryReplay(), clock=lambda: NOW)
    return Forwarder(FixedOrigin('https://worker.test.invalid'), auth,
                     clock=lambda: NOW, nonce_factory=lambda: 'origin-nonce')


def incoming(body=None, path='/internal/ocr/v2/jobs', method='POST', query='', extra=()):
    target = RequestTarget(method, path, query)
    signed = sign_headers(target, CLIENT, timestamp=str(int(NOW)), nonce='client-nonce')
    return IncomingRequest(target, signed + tuple(extra), body if body is not None else Body())


def forward(request, transport=None, sink=None, *, admission_open=True):
    transport = transport or FakeTransport()
    sink = sink or Sink()
    asyncio.run(make_forwarder().forward(request, transport, sink, admission_open=admission_open))
    return transport, sink


def test_exact_target_multipart_and_distinct_credentials_forwarded_once():
    multipart = [b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="synthetic.pdf"\r\n\r\n',
                 b'%PDF-small-fixture', b'\r\n--boundary--\r\n']
    request = incoming(Body(multipart), path='/api/v1/ocr/extract-text', query='b=2&a=%2F&a=one+two', extra=(
        ('Content-Type', 'multipart/form-data; boundary=boundary'), ('Host', 'evil.invalid'),
        ('Authorization', 'do-not-forward'), ('Cookie', 'private'), ('X-Maintenance-Bypass', 'yes'),
        ('X-Owner-Identity', 'synthetic-owner'), ('X-Request-ID', 'synthetic-request'),
    ))
    transport, sink = forward(request)
    outgoing = transport.request
    assert outgoing.origin.url == 'https://worker.test.invalid'
    assert outgoing.target.raw_target == '/api/v1/ocr/extract-text?b=2&a=%2F&a=one+two'
    wire_headers = dict(outgoing.headers)
    assert wire_headers['content-type'] == 'multipart/form-data; boundary=boundary'
    assert wire_headers['x-owner-identity'] == 'synthetic-owner'
    assert wire_headers['x-request-id'] == 'synthetic-request'
    literal = b'POST\n/api/v1/ocr/extract-text?b=2&a=%2F&a=one+two\n1800000000\norigin-nonce'
    assert wire_headers['x-worker-signature'] == hmac.new(ORIGIN.encode(), literal, hashlib.sha256).hexdigest()
    assert not {'host', 'authorization', 'cookie', 'x-maintenance-bypass'} & wire_headers.keys()
    assert transport.digest.digest() == hashlib.sha256(b''.join(multipart)).digest()
    assert sink.digest.digest() == hashlib.sha256(b'pdf').digest()
    assert request.body.closed and transport.closed
    assert transport.calls == 1


@pytest.mark.parametrize('origin', ['http://user:pass@host', 'https://host/path', 'https://host?x=1',
                                   'https://host#fragment', 'file:///tmp/pdf', '//host', '', 'https://host:bad'])
def test_only_configured_unambiguous_origin_allowed(origin):
    with pytest.raises(ProtocolError): FixedOrigin(origin)


def test_authentication_and_closed_gate_never_read_body_or_contact_upstream():
    for authenticated in [False, True]:
        request = incoming(Body([b'never-read']))
        if not authenticated: request = IncomingRequest(request.target, (), request.body)
        transport = FakeTransport()
        with pytest.raises(ProtocolError):
            forward(request, transport, admission_open=False)
        assert request.body.read == 0 and request.body.closed
        assert transport.calls == 0


def test_processing_get_closed_but_cancel_and_results_pass_to_downstream():
    for method, path in [('GET', '/api/v1/render/sessions/s/page/1'),
                         ('GET', '/new-processing')]:
        transport = FakeTransport()
        with pytest.raises(ProtocolError):
            forward(incoming(method=method, path=path), transport, admission_open=False)
        assert transport.calls == 0
    for method, path in [('POST', '/api/v1/jobs/job/cancel'),
                         ('GET', '/internal/ocr/v2/jobs/job/result'),
                         ('DELETE', '/api/v1/render/sessions/s')]:
        transport, _ = forward(incoming(method=method, path=path), admission_open=False)
        assert transport.calls == 1


def test_response_secret_and_hop_headers_not_exposed():
    response = UpstreamResponse(422, (('X-Worker-Signature', 'private'), ('Set-Cookie', 'private'),
                                    ('X-Internal-Secret', ORIGIN), ('Connection', 'content-type'),
                                    ('Content-Type', 'application/json'), ('Cache-Control', 'private')),
                                Body([b'{"detail":"invalid raster"}']))
    _, sink = forward(incoming(), FakeTransport(response))
    assert sink.head == (422, (('cache-control', 'private'),))


def test_header_injection_rejected_before_forwarding():
    transport = FakeTransport()
    with pytest.raises(ProtocolError):
        forward(incoming(extra=(('X-Request-ID', 'x\r\nInjected: true'),)), transport)
    assert transport.calls == 0


@pytest.mark.parametrize('fail', [ConnectionError('sensitive filename.pdf'), TimeoutError(ORIGIN)])
def test_ambiguous_upstream_failure_not_retried_or_logged(fail, caplog):
    request = incoming(Body([b'customer-document-bytes']))
    transport = FakeTransport(fail=fail)
    with pytest.raises(AmbiguousUpstreamError) as exc:
        forward(request, transport)
    assert exc.value.upstream_may_have_executed is True
    assert transport.calls == 1 and transport.closed and request.body.closed
    assert 'filename' not in str(exc.value) and ORIGIN not in str(exc.value)
    assert caplog.text == ''


def test_partial_response_disconnect_is_ambiguous_and_closes_everything():
    class DisconnectingBody(Body):
        async def __anext__(self):
            if self.read: raise ConnectionError('private-result.pdf')
            return await super().__anext__()
    response = UpstreamResponse(200, (), DisconnectingBody([b'partial']))
    transport, sink = FakeTransport(response), Sink()
    request = incoming()
    with pytest.raises(AmbiguousUpstreamError): forward(request, transport, sink)
    assert sink.count == len(b'partial') and sink.head == (200, ())
    assert transport.calls == 1 and transport.closed and request.body.closed and response.body.closed


def test_client_disconnect_preserves_ambiguity_and_backpressure():
    class DisconnectedSink(Sink):
        async def write(self, chunk): raise BrokenPipeError('disconnected client')
    response = UpstreamResponse(200, (), Body([b'one', b'two']))
    transport = FakeTransport(response)
    request = incoming()
    with pytest.raises(AmbiguousUpstreamError): forward(request, transport, DisconnectedSink())
    assert response.body.read == 1  # No read-ahead after the sink fails.
    assert request.body.closed and response.body.closed and transport.closed


def test_task_cancellation_is_not_reported_as_success_or_native_termination():
    async def scenario():
        entered = asyncio.Event()
        class SlowTransport(FakeTransport):
            @asynccontextmanager
            async def open_once(self, request):
                self.calls += 1
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield self.response
                finally: self.closed = True
        transport = SlowTransport()
        request = incoming()
        task = asyncio.create_task(make_forwarder().forward(request, transport, Sink(), admission_open=True))
        await entered.wait()
        task.cancel()
        with pytest.raises(ForwardingCancelled) as exc: await task
        assert exc.value.upstream_may_have_executed
        assert task.cancelled() and transport.calls == 1 and transport.closed and request.body.closed
    asyncio.run(scenario())


def test_redirect_never_followed_and_not_returned_as_success():
    transport = FakeTransport(UpstreamResponse(307, (('location', 'https://evil.invalid'),), Body()))
    sink = Sink()
    with pytest.raises(AmbiguousUpstreamError): forward(incoming(), transport, sink)
    assert transport.calls == 1 and sink.head is None and transport.closed


def test_large_logical_stream_has_bounded_live_memory_and_response_backpressure():
    # 64 MiB in each direction generated lazily; no large document allocation.
    def chunks():
        for _ in range(2048): yield b'x' * 32768
    request = incoming(Body(chunks()))
    response = UpstreamResponse(200, (), Body(chunks()))
    sink = Sink()
    tracemalloc.start()
    try:
        transport, _ = forward(request, FakeTransport(response), sink)
        _, peak = tracemalloc.get_traced_memory()
    finally: tracemalloc.stop()
    assert sink.count == 64 * 1024 * 1024
    assert request.body.read == response.body.read == 2048
    assert peak < 2 * 1024 * 1024
    assert request.body.closed and response.body.closed and transport.closed


def test_oversized_transport_chunk_fails_without_aggregation_or_retry():
    transport = FakeTransport()
    with pytest.raises(AmbiguousUpstreamError): forward(incoming(Body([b'x'*65537])), transport)
    assert transport.calls == 1 and transport.closed


def test_multipart_is_incrementally_parseable_with_original_filename_and_fields():
    class ParsingTransport(FakeTransport):
        @asynccontextmanager
        async def open_once(self, request):
            from starlette.formparsers import MultiPartParser
            from starlette.datastructures import Headers
            self.calls += 1
            try:
                form = await MultiPartParser(Headers(dict(request.headers)), request.body).parse()
                try:
                    assert form['language'] == 'eng'
                    assert form['file'].filename == 'synthetic.pdf'
                    assert await form['file'].read() == b'%PDF-small-fixture'
                finally: await form.close()
                yield self.response
            finally: self.closed = True
    body = b'--b\r\nContent-Disposition: form-data; name="file"; filename="synthetic.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-small-fixture\r\n--b\r\nContent-Disposition: form-data; name="language"\r\n\r\neng\r\n--b--\r\n'
    request = incoming(Body(body[i:i+7] for i in range(0, len(body), 7)), extra=(('content-type', 'multipart/form-data; boundary=b'),))
    transport, _ = forward(request, ParsingTransport())
    assert transport.calls == 1 and transport.closed and request.body.closed


def test_pre_attempt_dependency_error_is_redacted_and_body_closed():
    request = incoming()
    fwd = make_forwarder()
    def broken_clock(): raise RuntimeError('secret config path')
    fwd.clock = broken_clock
    transport = FakeTransport()
    with pytest.raises(ProtocolError) as exc:
        asyncio.run(fwd.forward(request, transport, Sink(), admission_open=True))
    assert 'secret config' not in str(exc.value)
    assert transport.calls == 0 and request.body.closed


def test_upstream_ownership_error_preserved_even_when_control_route_admitted():
    transport = FakeTransport(UpstreamResponse(403, (('content-type', 'application/json'),), Body([b'{"detail":"Forbidden"}'])))
    _, sink = forward(incoming(method='GET', path='/internal/ocr/v2/jobs/job/result'), transport, admission_open=False)
    assert sink.head == (403, (('content-type', 'application/json'),))
    assert sink.digest.digest() == hashlib.sha256(b'{"detail":"Forbidden"}').digest()


def test_noncanonical_or_injected_response_headers_fail_closed_after_attempt():
    transport = FakeTransport(UpstreamResponse(200, (('content-type', 'x\r\nInjected: yes'),), Body()))
    sink = Sink()
    with pytest.raises(AmbiguousUpstreamError): forward(incoming(), transport, sink)
    assert sink.head is None and transport.closed


def test_cleanup_failure_cannot_turn_ambiguous_work_into_success():
    class BadClose(Body):
        async def aclose(self):
            self.closed = True
            raise OSError('sensitive-file.pdf')
    request = incoming(BadClose())
    with pytest.raises(AmbiguousUpstreamError) as exc: forward(request)
    assert 'sensitive' not in str(exc.value) and request.body.closed


def test_compressed_response_keeps_encoding_with_unmodified_bytes():
    import gzip
    payload = gzip.compress(b'generated-response')
    transport = FakeTransport(UpstreamResponse(200, (('content-type', 'application/json'), ('content-encoding', 'gzip')), Body([payload])))
    _, sink = forward(incoming(extra=(('accept-encoding', 'gzip'),)), transport)
    assert dict(sink.head[1])['content-encoding'] == 'gzip'
    assert sink.digest.digest() == hashlib.sha256(payload).digest()


def test_body_hash_claim_preserved_and_resigned_without_reading_ahead():
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    request = IncomingRequest(target, sign_headers(target, CLIENT, timestamp='1800000000',
                              nonce='claimed-body', body_hash='claimed-digest'), Body([b'actual-bytes']))
    transport, _ = forward(request)
    outgoing = dict(transport.request.headers)
    assert outgoing['x-worker-body-hash'] == 'claimed-digest'
    literal = b'POST\n/internal/ocr/v2/jobs\n1800000000\norigin-nonce\nclaimed-digest'
    assert outgoing['x-worker-signature'] == hmac.new(ORIGIN.encode(), literal, hashlib.sha256).hexdigest()


def test_open_failure_before_request_read_is_still_conservatively_ambiguous():
    class OpeningFailure(FakeTransport):
        @asynccontextmanager
        async def open_once(self, request):
            self.calls += 1
            try:
                raise ConnectionError('private socket detail')
                yield self.response
            finally: self.closed = True
    transport, request = OpeningFailure(), incoming(Body([b'unread']))
    with pytest.raises(AmbiguousUpstreamError): forward(request, transport)
    assert request.body.read == 0 and request.body.closed and transport.closed
    assert transport.calls == 1


def test_client_upload_disconnect_is_ambiguous_without_resubmission():
    class UploadDisconnect(Body):
        async def __anext__(self):
            if self.read: raise BrokenPipeError('private upload name')
            return await super().__anext__()
    request = incoming(UploadDisconnect([b'partial-multipart']))
    transport, sink = FakeTransport(), Sink()
    with pytest.raises(AmbiguousUpstreamError): forward(request, transport, sink)
    assert transport.calls == 1 and transport.closed and request.body.closed
    assert sink.head is None

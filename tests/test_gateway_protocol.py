"""Isolated protocol tests: no live listener, broker, storage or customer data."""
import asyncio
import hashlib
import hmac
import math

import pytest

from gateway_protocol.auth import Credentials, ClientAuthenticator, sign_headers
from gateway_protocol.errors import AuthenticationError, ProtocolError, ReplayUnavailable
from gateway_protocol.routes import RouteKind, classify, require_admission
from gateway_protocol.target import RequestTarget

CLIENT = 'test-client-' + 'a' * 32
ORIGIN = 'test-origin-' + 'b' * 32
NOW = 1_800_000_000.0


class MemoryReplay:
    """Test-only atomic store; deliberately not shipped as a deployment fallback."""
    def __init__(self):
        self.entries = {}

    async def reserve(self, nonce, *, now, expires_at):
        if self.entries.get(nonce, -math.inf) > now:
            return False
        self.entries[nonce] = expires_at
        return True


def run(coro):
    return asyncio.run(coro)


def headers(target, *, secret=CLIENT, timestamp=str(int(NOW)), nonce='test-nonce'):
    return sign_headers(target, secret, timestamp=timestamp, nonce=nonce)


def test_exact_backend_canonical_representation_and_literal_signature():
    target = RequestTarget('post', '/api/v1/ocr/extract-text', 'b=2&a=one+two&a=%2F')
    literal = b'POST\n/api/v1/ocr/extract-text?b=2&a=one+two&a=%2F\n1800000000\ntest-nonce\nclaimed-hash'
    assert target.canonical('1800000000', 'test-nonce', 'claimed-hash') == literal
    signed = sign_headers(target, CLIENT, timestamp='1800000000', nonce='test-nonce', body_hash='claimed-hash')
    assert dict(signed)['x-worker-signature'] == hmac.new(CLIENT.encode(), literal, hashlib.sha256).hexdigest()


def test_encoded_unreserved_path_is_preserved_but_signed_decoded():
    target = RequestTarget('GET', '/api/v1/jobs/%61bc', 'x=%252F&x=+&empty=')
    assert target.raw_target == '/api/v1/jobs/%61bc?x=%252F&x=+&empty='
    assert target.canonical('1800000000', 'n') == b'GET\n/api/v1/jobs/abc?x=%252F&x=+&empty=\n1800000000\nn'
    assert classify(target) is RouteKind.CONTROL


@pytest.mark.parametrize('path,query', [
    ('https://evil.invalid/a', ''), ('//evil.invalid/a', ''),
    ('/a/../health', ''), ('/a/%2e%2e/health', ''), ('/a%2fhealth', ''),
    ('/a%5Chealth', ''), ('/a%252fhealth', ''), ('/a\\health', ''),
    ('/a//health', ''), ('/a#x', ''), ('/a?x=y', ''), ('/a%', ''),
    ('/a\r\nx:y', ''), ('/a', 'x=\r\nHeader:y'), ('/a', 'x=%zz'), ('/a', 'x=#f'),
])
def test_ambiguous_or_injected_targets_are_rejected(path, query):
    with pytest.raises(ProtocolError):
        RequestTarget('GET', path, query)


@pytest.mark.parametrize('method', ['', 'GET\nPOST', 'G ET', 'CONNECT', 'TRACE'])
def test_unsupported_methods_fail_closed(method):
    with pytest.raises(ProtocolError):
        RequestTarget(method, '/')


@pytest.mark.parametrize('client,origin', [('', ORIGIN), (CLIENT, ''), (CLIENT, CLIENT), (' secret ', ORIGIN)])
def test_no_missing_shared_or_implicit_credentials(client, origin):
    with pytest.raises(ProtocolError):
        Credentials(client, origin)


def test_credentials_repr_redacted():
    assert CLIENT not in repr(Credentials(CLIENT, ORIGIN))
    assert ORIGIN not in repr(Credentials(CLIENT, ORIGIN))


def test_auth_success_then_replay_and_separate_origin_key():
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
    run(auth.authenticate(target, headers(target)))
    with pytest.raises(AuthenticationError):
        run(auth.authenticate(target, headers(target)))
    with pytest.raises(AuthenticationError):
        run(auth.authenticate(target, headers(target, secret=ORIGIN, nonce='origin-key')))


@pytest.mark.parametrize('timestamp', ['nan', 'inf', '-inf', 'bad', '', str(NOW-301), str(NOW+301)])
def test_clock_skew_and_nonfinite_fail(timestamp):
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    signed = list(headers(target))
    signed = [(key, timestamp if key == 'x-worker-timestamp' else val) for key, val in signed]
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
    with pytest.raises(AuthenticationError):
        run(auth.authenticate(target, signed))


def test_future_nonce_retained_through_entire_acceptance_window():
    now = [NOW]
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    store = MemoryReplay()
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), store, clock=lambda: now[0])
    signed = headers(target, timestamp=str(int(NOW+300)))
    run(auth.authenticate(target, signed))
    now[0] = NOW + 599
    with pytest.raises(AuthenticationError):
        run(auth.authenticate(target, signed))


def test_invalid_signature_cannot_poison_nonce():
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
    with pytest.raises(AuthenticationError):
        run(auth.authenticate(target, headers(target, secret=ORIGIN)))
    run(auth.authenticate(target, headers(target)))


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'bad-signature', 'bad-nonce', 'injected-header'])
def test_malformed_headers_rejected(change):
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    signed = list(headers(target))
    if change == 'missing': signed.pop()
    elif change == 'duplicate': signed.append(('X-Worker-Nonce', 'duplicate'))
    elif change == 'bad-signature': signed[0] = ('x-worker-signature', 'z'*64)
    elif change == 'bad-nonce': signed = [(k, '' if k == 'x-worker-nonce' else v) for k,v in signed]
    else: signed.append(('content-type', 'test\r\nInjected: yes'))
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
    with pytest.raises(ProtocolError):
        run(auth.authenticate(target, signed))


def test_store_failure_has_no_memory_fallback_or_secret_error():
    class Broken:
        async def reserve(self, *args, **kwargs):
            raise RuntimeError('secret database details')
    target = RequestTarget('POST', '/internal/ocr/v2/jobs')
    auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), Broken(), clock=lambda: NOW)
    with pytest.raises(ReplayUnavailable) as exc:
        run(auth.authenticate(target, headers(target)))
    assert 'secret database' not in str(exc.value)


@pytest.mark.parametrize('method,path,kind', [
    ('POST', '/api/v1/office/convert', RouteKind.PROCESSING),
    ('POST', '/api/v1/office-to-pdf/convert', RouteKind.PROCESSING),
    ('POST', '/api/v1/ocr/extract-text', RouteKind.PROCESSING),
    ('POST', '/api/v1/ocr/to-text-pdf', RouteKind.PROCESSING),
    ('POST', '/api/v1/ocr/to-text-pdf-r2', RouteKind.PROCESSING),
    ('POST', '/api/v1/markdown/convert', RouteKind.PROCESSING),
    ('POST', '/internal/ocr/v2/text', RouteKind.PROCESSING),
    ('POST', '/internal/ocr/v2/markup/preview', RouteKind.PROCESSING),
    ('POST', '/internal/ocr/v2/jobs', RouteKind.PROCESSING),
    ('POST', '/api/v1/editor/extract', RouteKind.PROCESSING),
    ('POST', '/api/v1/editor/compile', RouteKind.PROCESSING),
    ('POST', '/api/v1/markup/highlight', RouteKind.PROCESSING),
    ('POST', '/api/v1/markup/underline', RouteKind.PROCESSING),
    ('POST', '/api/v1/markup/strikeout', RouteKind.PROCESSING),
    ('POST', '/api/v1/jobs/test', RouteKind.PROCESSING),
    ('POST', '/api/v1/jobs/submit', RouteKind.PROCESSING),
    ('POST', '/api/v1/render/page', RouteKind.PROCESSING),
    ('POST', '/api/v1/render/sessions', RouteKind.PROCESSING),
    ('GET', '/api/v1/render/sessions/abc/page/1', RouteKind.PROCESSING),
    ('POST', '/api/v1/analyzer/python-ast', RouteKind.PROCESSING),
    ('POST', '/api/v1/analyzer/analyze', RouteKind.PROCESSING),
    ('POST', '/api/v1/metadata/read', RouteKind.PROCESSING),
    ('POST', '/api/v1/metadata/write', RouteKind.PROCESSING),
    ('POST', '/api/v1/metadata/preserve', RouteKind.PROCESSING),
    ('POST', '/api/v1/redact', RouteKind.PROCESSING),
    ('POST', '/api/v1/sign', RouteKind.PROCESSING),
    ('GET', '/health/ready', RouteKind.CONTROL),
    ('GET', '/health/live', RouteKind.CONTROL),
    ('GET', '/health', RouteKind.CONTROL),
    ('GET', '/', RouteKind.CONTROL),
    ('GET', '/internal/ocr/v2/capabilities', RouteKind.CONTROL),
    ('GET', '/api/v1/ocr/languages', RouteKind.CONTROL),
    ('GET', '/api/v1/render/metrics', RouteKind.CONTROL),
    ('GET', '/api/v1/jobs', RouteKind.CONTROL),
    ('GET', '/api/v1/jobs/abc', RouteKind.CONTROL),
    ('GET', '/internal/ocr/v2/jobs/abc', RouteKind.CONTROL),
    ('GET', '/internal/ocr/v2/jobs/abc/result', RouteKind.CONTROL),
    ('GET', '/api/v1/editor/jobs/abc/download', RouteKind.CONTROL),
    ('GET', '/api/v1/markup/jobs/abc/download', RouteKind.CONTROL),
    ('POST', '/api/v1/jobs/abc/cancel', RouteKind.CONTROL),
    ('POST', '/internal/ocr/v2/jobs/abc/cancel', RouteKind.CONTROL),
    ('DELETE', '/api/v1/render/sessions/abc', RouteKind.CONTROL),
    ('GET', '/health-something', RouteKind.UNKNOWN),
    ('POST', '/health/ready', RouteKind.UNKNOWN),
    ('GET', '/api/v1/new-render', RouteKind.UNKNOWN),
    ('POST', '/unknown', RouteKind.UNKNOWN),
])
def test_explicit_route_policy(method, path, kind):
    target = RequestTarget(method, path)
    assert classify(target) is kind
    if kind is RouteKind.CONTROL:
        require_admission(target, admission_open=False)
    else:
        with pytest.raises(ProtocolError):
            require_admission(target, admission_open=False)
    if kind is not RouteKind.UNKNOWN:
        require_admission(target, admission_open=True)


def test_query_cannot_bypass_closed_gate_and_trailing_slash_still_processing():
    for target in [RequestTarget('POST', '/api/v1/jobs/submit', 'maintenance_bypass=1'),
                   RequestTarget('POST', '/internal/ocr/v2/jobs/')]:
        with pytest.raises(ProtocolError):
            require_admission(target, admission_open=False)


def test_worker_verifier_accepts_origin_signature_without_body_integrity_claim(monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    from app.core.security import WorkerAuthMiddleware
    monkeypatch.setenv('WORKER_SHARED_SECRET', ORIGIN)
    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.setattr('app.core.security.time.time', lambda: NOW)
    target = RequestTarget('POST', '/internal/ocr/v2/jobs', 'b=2&a=%2F&a=one+two')
    signed = sign_headers(target, ORIGIN, timestamp='1800000000', nonce='compat-real-worker', body_hash='not-an-actual-body-hash')
    request = Request({'type':'http', 'method':'POST', 'path':target.path,
                       'query_string':target.raw_query.encode(), 'headers':[(k.encode(),v.encode()) for k,v in signed],
                       'scheme':'http', 'server':('test.invalid',80)})
    async def accepted(request):
        return Response(status_code=204)
    response = run(WorkerAuthMiddleware(accepted).dispatch(request, accepted))
    assert response.status_code == 204
    assert run(WorkerAuthMiddleware(accepted).dispatch(request, accepted)).status_code == 401


def test_policy_covers_every_current_mounted_route_without_importing_worker_app():
    """A newly mounted processing route cannot silently inherit a control allowlist."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    main = ast.parse((root/'app/main.py').read_text())
    mounted = {node.args[0].id for node in ast.walk(main)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == 'include_router' and node.args}
    modules = [node.module for node in main.body if isinstance(node, ast.ImportFrom)
               and any(alias.asname in mounted for alias in node.names)]
    assert len(modules) == 15
    count = 0
    for module in [None, *modules]:
        tree = main if module is None else ast.parse((root/(module.replace('.', '/')+'.py')).read_text())
        prefix = ''
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'APIRouter':
                prefix = next((ast.literal_eval(kw.value) for kw in node.keywords if kw.arg == 'prefix'), '')
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {'get','post','put','patch','delete'}): continue
                path = prefix + ast.literal_eval(decorator.args[0])
                import re
                path = re.sub(r'\{[^}]+\}', 'synthetic-id', path)
                target = RequestTarget(decorator.func.attr, path)
                kind = classify(target)
                assert kind is not RouteKind.UNKNOWN, (module, path)
                # Every POST except an explicit cancel endpoint does processing.
                if target.method == 'POST' and not path.endswith('/cancel'):
                    assert kind is RouteKind.PROCESSING, path
                count += 1
    assert count >= 40  # Guard an accidentally empty discovery result.


@pytest.mark.parametrize('path', ['/bad%FF', '/bad%E0%A4', '/bad%C0%AF'])
def test_invalid_utf8_path_is_typed_protocol_failure(path):
    with pytest.raises(ProtocolError): RequestTarget('GET', path)


def test_simultaneous_authenticated_replays_only_one_succeeds():
    async def scenario():
        auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
        target = RequestTarget('POST', '/internal/ocr/v2/jobs')
        results = await asyncio.gather(*(auth.authenticate(target, headers(target)) for _ in range(8)), return_exceptions=True)
        assert sum(value is None for value in results) == 1
        assert sum(isinstance(value, AuthenticationError) for value in results) == 7
    run(scenario())


def test_changed_method_path_and_query_invalidate_authentication():
    original = RequestTarget('POST', '/api/v1/ocr/extract-text', 'a=1&b=2')
    for changed in [RequestTarget('GET', original.raw_path, original.raw_query),
                    RequestTarget('POST', '/api/v1/ocr/to-text-pdf', original.raw_query),
                    RequestTarget('POST', original.raw_path, 'b=2&a=1')]:
        auth = ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), clock=lambda: NOW)
        with pytest.raises(AuthenticationError): run(auth.authenticate(changed, headers(original)))


@pytest.mark.parametrize('skew', [0, -1, True, float('nan'), float('inf'), 301])
def test_invalid_replay_window_rejected(skew):
    with pytest.raises(ProtocolError):
        ClientAuthenticator(Credentials(CLIENT, ORIGIN), MemoryReplay(), skew_seconds=skew)


def test_real_worker_authenticates_encoded_path_and_still_does_not_hash_body(monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    from app.core.security import WorkerAuthMiddleware
    monkeypatch.setenv('WORKER_SHARED_SECRET', ORIGIN)
    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.setattr('app.core.security.time.time', lambda: NOW)
    target = RequestTarget('POST', '/internal/ocr/v2/%74ext', 'a=%2F&b=one+two')
    observed = []
    async def accepted(request):
        observed.append(await request.body())
        return Response(status_code=204)
    async def scenario():
        for index, body in enumerate([b'original-body', b'different-body']):
            signed = sign_headers(target, ORIGIN, timestamp='1800000000', nonce=f'body-limit-{index}', body_hash='same-claimed-hash')
            async def receive(): return {'type':'http.request', 'body':body, 'more_body':False}
            request = Request({'type':'http', 'method':'POST', 'path':target.path,
                               'raw_path':target.raw_path.encode(), 'query_string':target.raw_query.encode(),
                               'headers':[(k.encode(),v.encode()) for k,v in signed],
                               'scheme':'http', 'server':('test.invalid',80)}, receive)
            assert (await WorkerAuthMiddleware(accepted).dispatch(request, accepted)).status_code == 204
    run(scenario())
    assert observed == [b'original-body', b'different-body']

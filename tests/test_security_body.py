"""Raw ASGI body-integrity regressions; no broker, storage or native processing."""
import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import tracemalloc
import uuid

import pytest
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

from app.core import security

SECRET = "f15-synthetic-secret"


def signed(body=b"", *, nonce=None, timestamp=None, digest=None, method="POST", target="/probe?a=%2F"):
    timestamp = str(time.time() if timestamp is None else timestamp)
    nonce = nonce or uuid.uuid4().hex
    digest = hashlib.sha256(body).hexdigest() if digest is None else digest
    canonical = f"{method}\n{target}\n{timestamp}\n{nonce}\n{digest}"
    return {"x-worker-timestamp": timestamp, "x-worker-nonce": nonce,
            "x-worker-body-hash": digest,
            "x-worker-signature": hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()}


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    monkeypatch.setenv("WORKER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("REDIS_URL", raising=False)
    security._SEEN_NONCES.clear()


async def invoke(app, messages, headers, *, method="POST", target="/probe?a=%2F", receive_override=None):
    source = iter(messages)
    async def receive():
        try:
            return next(source)
        except StopIteration:
            await asyncio.Event().wait()
    output = []
    async def send(message):
        output.append(message)
    path, _, query = target.partition("?")
    scope = {"type": "http", "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "scheme": "http",
             "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1234),
             "headers": [(key.encode(), value.encode()) for key, value in headers.items()]}
    await app(scope, receive_override or receive, send)
    status = next(message["status"] for message in output if message["type"] == "http.response.start")
    return status, b"".join(message.get("body", b"") for message in output if message["type"] == "http.response.body")


def chunk(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


class Echo:
    def __init__(self):
        self.calls = 0
        self.max_chunk = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        digest = hashlib.sha256()
        size = 0
        while True:
            message = await receive()
            data = message.get("body", b"")
            size += len(data)
            self.max_chunk = max(self.max_chunk, len(data))
            digest.update(data)
            if not message.get("more_body", False):
                break
        from fastapi.responses import JSONResponse
        await JSONResponse({"size": size, "digest": digest.hexdigest()})(scope, receive, send)


@pytest.mark.parametrize("body", [b"", b'{"value":1}', b"multipart\x00\r\nraw bytes"])
def test_actual_bytes_and_empty_body(body):
    echo = Echo()
    status, output = asyncio.run(invoke(security.WorkerAuthMiddleware(echo), [chunk(body)], signed(body)))
    assert status == 200
    assert json.loads(output) == {"size": len(body), "digest": hashlib.sha256(body).hexdigest()}
    assert echo.calls == 1


@pytest.mark.parametrize("body,changed", [(b'{"value":1}', b'{"value":2}'), (b"file contents", b"file contentX"),
                                         (b"--boundary\r\nfield=value", b"--boundarY\r\nfield=value")])
def test_changed_body_is_rejected_before_route_or_nonce(body, changed):
    echo = Echo()
    middleware = security.WorkerAuthMiddleware(echo)
    headers = signed(body)
    assert asyncio.run(invoke(middleware, [chunk(changed)], headers))[0] == 401
    assert echo.calls == 0
    assert headers["x-worker-nonce"] not in security._SEEN_NONCES
    assert asyncio.run(invoke(middleware, [chunk(body)], headers))[0] == 200


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "A" * 64, "x" * 64, "0" * 64])
def test_missing_malformed_or_fabricated_digest(digest):
    echo = Echo()
    headers = signed(b"actual", digest=digest)
    assert asyncio.run(invoke(security.WorkerAuthMiddleware(echo), [chunk(b"actual")], headers))[0] == 401
    assert echo.calls == 0
    assert not security._SEEN_NONCES


def test_legacy_hashless_hmac_is_rejected():
    headers = signed()
    del headers["x-worker-body-hash"]
    canonical = f"POST\n/probe?a=%2F\n{headers['x-worker-timestamp']}\n{headers['x-worker-nonce']}"
    headers["x-worker-signature"] = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    echo = Echo()
    assert asyncio.run(invoke(security.WorkerAuthMiddleware(echo), [chunk(b"")], headers))[0] == 401
    assert echo.calls == 0


@pytest.mark.parametrize("change", ["method", "path", "query", "signature"])
def test_auth_target_and_signature_cannot_change(change):
    headers = signed(b"value")
    method = "GET" if change == "method" else "POST"
    target = "/other?a=%2F" if change == "path" else "/probe?a=%2f" if change == "query" else "/probe?a=%2F"
    if change == "signature":
        headers["x-worker-signature"] = "0" * 64
    echo = Echo()
    assert asyncio.run(invoke(security.WorkerAuthMiddleware(echo), [chunk(b"value")], headers, method=method, target=target))[0] == 401
    assert not security._SEEN_NONCES
    assert echo.calls == 0


def test_real_fastapi_multipart_parser_receives_identical_verified_body():
    app = FastAPI()
    app.add_middleware(security.WorkerAuthMiddleware)
    @app.post("/multipart")
    async def endpoint(file: UploadFile = File(...), language: str = Form(...)):
        return {"name": file.filename, "content": (await file.read()).decode(), "language": language}
    client = TestClient(app)
    outgoing = client.build_request("POST", "/multipart", files={"file": ("small.pdf", b"%PDF-synthetic", "application/pdf")}, data={"language": "eng"})
    body = outgoing.read()
    outgoing.headers.update(signed(body, target="/multipart"))
    assert client.send(outgoing).json() == {"name": "small.pdf", "content": "%PDF-synthetic", "language": "eng"}


def test_large_permitted_stream_has_bounded_replay_memory_and_cleanup(monkeypatch, tmp_path):
    files = []
    original = security.tempfile.TemporaryFile
    def track(*args, **kwargs):
        result = original(*args, **kwargs)
        files.append(result)
        assert os.fstat(result.fileno()).st_mode & 0o777 == 0o600
        return result
    monkeypatch.setattr(security.tempfile, "TemporaryFile", track)
    echo = Echo()
    middleware = security.WorkerAuthMiddleware(echo, temp_dir=str(tmp_path))
    part = b"s" * 65536
    expected = hashlib.sha256()
    for _ in range(64):
        expected.update(part)
    headers = signed(digest=expected.hexdigest())
    tracemalloc.start()
    status, output = asyncio.run(invoke(middleware, (chunk(part, index < 63) for index in range(64)), headers))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert status == 200 and json.loads(output)["size"] == 4 * 1024 * 1024
    assert echo.max_chunk <= security.AUTH_BODY_CHUNK_BYTES
    assert peak < 2 * 1024 * 1024, f"authentication buffered the 4 MiB body: peak={peak}"
    assert files and all(file.closed for file in files)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("case", ["oversize", "truncated", "disconnect", "timeout", "cancel"])
def test_incomplete_or_over_limit_never_routes_and_cleans_spool(case, monkeypatch, tmp_path):
    files = []
    original = security.tempfile.TemporaryFile
    def track(*args, **kwargs):
        result = original(*args, **kwargs); files.append(result); return result
    monkeypatch.setattr(security.tempfile, "TemporaryFile", track)
    echo = Echo()
    middleware = security.WorkerAuthMiddleware(echo, max_body_bytes=8, body_timeout=0.02, temp_dir=str(tmp_path))
    headers = signed(b"12345678")
    messages = [chunk(b"1234", True)]
    if case == "oversize": messages.append(chunk(b"56789"))
    if case == "truncated":
        headers["content-length"] = "8"; messages.append(chunk(b"5"))
    if case == "disconnect": messages.append({"type": "http.disconnect"})
    async def run():
        task = asyncio.create_task(invoke(middleware, messages, headers))
        if case == "cancel":
            while not files: await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            status, _ = await task
            assert status == {"oversize": 413, "truncated": 400, "disconnect": 400, "timeout": 408}[case]
    asyncio.run(run())
    assert echo.calls == 0 and not security._SEEN_NONCES
    assert all(file.closed for file in files) and not list(tmp_path.iterdir())
    # Capacity and parent health recover after every rejection/cancellation.
    middleware.body_timeout = 1
    assert asyncio.run(invoke(middleware, [chunk(b"12345678")], headers))[0] == 200


def test_concurrent_nonce_reservation_and_independent_body_state(monkeypatch):
    from redis.asyncio import Redis
    stored = set()
    class FakeRedis:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def set(self, key, value, **kwargs):
            assert kwargs["nx"] and kwargs["ex"] >= 600
            await asyncio.sleep(0)
            if key in stored: return False
            stored.add(key); return True
    monkeypatch.setenv("REDIS_URL", "redis://synthetic.invalid")
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: FakeRedis())
    echo = Echo(); middleware = security.WorkerAuthMiddleware(echo)
    headers = signed(b"one", nonce="shared-nonce")
    async def run():
        assert (await invoke(middleware, [chunk(b"bad")], headers))[0] == 401
        assert not stored, "invalid bytes must not reserve a Redis nonce"
        replies = await asyncio.gather(*(invoke(middleware, [chunk(b"one")], headers) for _ in range(3)))
        assert sorted(status for status, _ in replies) == [200, 401, 401]
        replies = await asyncio.gather(*(invoke(middleware, [chunk(str(i).encode())], signed(str(i).encode())) for i in range(3)))
        assert all(status == 200 for status, _ in replies)
        assert [json.loads(output)["digest"] for _, output in replies] == [hashlib.sha256(str(i).encode()).hexdigest() for i in range(3)]
    asyncio.run(run())
    assert echo.calls == 4


def test_fixed_go_python_signature_vector(monkeypatch):
    # Identical public vector in internal/worker/body_test.go, independent of
    # JSON serializer output and of clocks/nonces at test execution time.
    body = '{"operation":"ocr","name":"café"}\n'.encode()
    digest = hashlib.sha256(body).hexdigest()
    assert digest == "c42873a7e4f262babfcc423f3d95d9b8f02c291b62909b16bf8ed583dffe7cbd"
    canonical = f"POST\n/internal/ocr/v2/jobs?b=2&a=%2F\n1800000000\nf15-cross-language\n{digest}"
    signature = hmac.new(b"f15-public-vector-secret", canonical.encode(), hashlib.sha256).hexdigest()
    assert signature == "6ab65000844eb3a4173ab80ebe8a7dfb79e150d2be4f2cbcbb218f7c265d3128"
    monkeypatch.setenv("WORKER_SHARED_SECRET", "f15-public-vector-secret")
    monkeypatch.setattr(security.time, "time", lambda: 1800000000)
    headers = {"x-worker-timestamp": "1800000000", "x-worker-nonce": "f15-cross-language",
               "x-worker-signature": signature, "x-worker-body-hash": digest}
    assert asyncio.run(invoke(security.WorkerAuthMiddleware(Echo()), [chunk(body)], headers, target="/internal/ocr/v2/jobs?b=2&a=%2F"))[0] == 200


def test_capacity_is_bounded_but_empty_controls_still_work():
    echo = Echo(); middleware = security.WorkerAuthMiddleware(echo)
    async def run():
        release = asyncio.Event()
        arrivals = 0
        async def held_receive():
            nonlocal arrivals
            arrivals += 1
            if arrivals <= 4:
                return chunk(b"x", True)
            await release.wait()
            return chunk(b"")
        # Each stream supplies its first chunk once, then waits on its own
        # receiver: all four private spools must be independently occupied.
        def receiver():
            first = True
            async def receive():
                nonlocal first
                if first:
                    first = False
                    return await held_receive()
                await release.wait()
                return chunk(b"")
            return receive
        tasks = [asyncio.create_task(invoke(middleware, [], signed(b"x"), receive_override=receiver())) for _ in range(4)]
        while arrivals < 4:
            await asyncio.sleep(0)
        assert not middleware._slots.acquire(blocking=False)
        try:
            assert (await invoke(middleware, [chunk(b"x")], signed(b"x")))[0] == 503
            assert (await invoke(middleware, [chunk(b"")], signed()))[0] == 200
        finally:
            release.set()
            assert all(status == 200 for status, _ in await asyncio.gather(*tasks))
        assert (await invoke(middleware, [chunk(b"x")], signed(b"x")))[0] == 200
    asyncio.run(run())


def test_downstream_failure_releases_verified_spool():
    async def fails(scope, receive, send):
        raise RuntimeError("controlled handler failure before body read")
    middleware = security.WorkerAuthMiddleware(fails)
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(middleware, [chunk(b"x")], signed(b"x")))
    for _ in range(4):
        assert middleware._slots.acquire(blocking=False)


def test_cancellation_waits_for_file_operation_before_cleanup():
    started = threading.Event(); finish = threading.Event()
    def bounded_write():
        started.set(); assert finish.wait(1)
    async def run():
        task = asyncio.create_task(security._file_io(bounded_write))
        while not started.is_set(): await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())

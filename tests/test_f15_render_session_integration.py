"""Strict-authentication -> real render routes -> isolated native metadata.

Only synthetic multipart bodies are buffered by these test clients. Production
authentication still stages/replays bounded chunks. No Redis or public services.
"""
import asyncio
import hashlib
import io
import os

import httpx
from PIL import Image
import pytest

from app.core import security
from app.main import app
from app.api.tools.render import session, session_metadata as metadata
from tests.test_security import generate_headers
from tests.test_render_session_metadata import (
    manager, child_probe, small_pdf, assert_clean, assert_probe_reaped,
)

SECRET = "f15-render-synthetic-secret"
CREATE = "/api/v1/render/sessions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def authentication(monkeypatch):
    monkeypatch.setenv("WORKER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("REDIS_URL", raising=False)
    security._SEEN_NONCES.clear()
    opened = []
    original = security.tempfile.TemporaryFile

    def track(*args, **kwargs):
        spool = original(*args, **kwargs)
        opened.append(spool)
        assert os.fstat(spool.fileno()).st_mode & 0o777 == 0o600
        return spool

    monkeypatch.setattr(security.tempfile, "TemporaryFile", track)
    yield opened
    assert all(spool.closed for spool in opened)
    security._SEEN_NONCES.clear()


async def signed_request(client, method="POST", target=CREATE, data=None):
    kwargs = {} if data is None else {"files": {"file": ("small.pdf", data, "application/pdf")}}
    outgoing = client.build_request(method, target, **kwargs)
    body = await outgoing.aread()
    outgoing.headers.update(generate_headers(
        method, target, secret=SECRET, body_hash=hashlib.sha256(body).hexdigest(),
    ))
    return outgoing, body


def client():
    # The complete application middleware stack and actual router are used;
    # lifespan is intentionally omitted so no background/shared services start.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_signed_session_lifecycle_and_empty_body_controls(manager, authentication):
    data = small_pdf(2)
    async with client() as http:
        outgoing, _ = await signed_request(http, data=data)
        created = await http.send(outgoing)
        assert created.status_code == 200, created.text
        value = created.json()
        assert set(value) == {"session_id", "page_count", "file_size"}
        assert value["page_count"] == 2 and value["file_size"] == len(data)
        identifier = value["session_id"]
        assert manager.get(identifier).file_path.read_bytes() == data
        duplicate, _ = await signed_request(http, data=data)
        assert (await http.send(duplicate)).json()["session_id"] == identifier
        outgoing, _ = await signed_request(http, "GET", f"{CREATE}/{identifier}/page/2?dpi=72")
        rendered = await http.send(outgoing)
        assert rendered.status_code == 200, rendered.text
        image = Image.open(io.BytesIO(rendered.content))
        assert image.format == "JPEG" and image.size == (200, 100)
        outgoing, _ = await signed_request(http, "DELETE", f"{CREATE}/{identifier}")
        assert (await http.send(outgoing)).json() == {"deleted": True}
    assert not manager._sessions and not list(session.SESSION_ROOT.iterdir())
    assert authentication and all(spool.closed for spool in authentication)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["missing", "malformed", "tampered", "hmac"])
async def test_authentication_rejects_before_session_creation(manager, monkeypatch, tmp_path, mutation):
    inspected = False

    async def forbidden(path):
        nonlocal inspected
        inspected = True
        raise AssertionError("Unauthenticated content reached metadata")

    monkeypatch.setattr(session, "inspect_page_count", forbidden)
    async with client() as http:
        outgoing, body = await signed_request(http, data=small_pdf())
        nonce = outgoing.headers["X-Worker-Nonce"]
        if mutation == "missing":
            del outgoing.headers["X-Worker-Body-Hash"]
        elif mutation == "malformed":
            outgoing.headers["X-Worker-Body-Hash"] = "A" * 64
        elif mutation == "hmac":
            outgoing.headers["X-Worker-Signature"] = "0" * 64
        else:
            outgoing = httpx.Request("POST", outgoing.url, headers=outgoing.headers,
                                     content=body.replace(b"%PDF-", b"%PDX-", 1))
        assert (await http.send(outgoing)).status_code == 401
    assert not inspected and nonce not in security._SEEN_NONCES
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_replayed_creation_cannot_publish_again(manager, tmp_path):
    async with client() as http:
        outgoing, body = await signed_request(http, data=small_pdf())
        response = await http.send(outgoing)
        assert response.status_code == 200
        manager.delete(response.json()["session_id"])
        replay = httpx.Request("POST", outgoing.url, headers=outgoing.headers, content=body)
        assert (await http.send(replay)).status_code == 401
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("data", [b"not a PDF", b"%PDF-1.4\nbroken"])
async def test_signed_invalid_pdf_has_no_session_and_recovers(manager, tmp_path, data):
    async with client() as http:
        outgoing, _ = await signed_request(http, data=data)
        invalid = await http.send(outgoing)
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "INVALID_DOCUMENT"
        assert_clean(manager, tmp_path)
        outgoing, _ = await signed_request(http, data=small_pdf())
        assert (await http.send(outgoing)).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["hang", "crash_tree"])
async def test_authenticated_child_failure_cleans_both_stages_and_recovers(
    manager, child_probe, monkeypatch, tmp_path, authentication, mode,
):
    child_probe["mode"] = mode
    monkeypatch.setattr(metadata, "METADATA_TIMEOUT_SECONDS", 3)
    async with client() as http:
        outgoing, _ = await signed_request(http, data=small_pdf())
        response = await http.send(outgoing)
        assert response.status_code == (504 if mode == "hang" else 503)
        assert response.json()["detail"]["code"] == (
            "SESSION_METADATA_TIMEOUT" if mode == "hang" else "SESSION_METADATA_FAILED"
        )
        assert_probe_reaped(child_probe)
        assert_clean(manager, tmp_path)
        assert authentication and all(spool.closed for spool in authentication)
        child_probe["mode"] = "valid"
        outgoing, _ = await signed_request(http, data=small_pdf())
        assert (await http.send(outgoing)).status_code == 200


@pytest.mark.anyio
async def test_signed_native_capacity_rejection_and_recovery(manager, tmp_path):
    from app.core.native_isolation import _capacity
    async with client() as http:
        outgoing, _ = await signed_request(http, data=small_pdf())
        with _capacity(lambda: None, 0):
            response = await http.send(outgoing)
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "SESSION_METADATA_BUSY"
        assert response.headers["retry-after"] == "2"
        assert_clean(manager, tmp_path)
        outgoing, _ = await signed_request(http, data=small_pdf())
        assert (await http.send(outgoing)).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True], ids=["disconnect", "task-cancellation"])
async def test_verified_request_disconnect_or_cancel_reaps_metadata(
    manager, child_probe, tmp_path, authentication, cancel,
):
    child_probe["mode"] = "hang"
    async with client() as http:
        outgoing, body = await signed_request(http, data=small_pdf())
    position = 0
    output = []

    async def receive():
        nonlocal position
        if position < len(body):
            end = min(position + 64, len(body))
            part, position = body[position:end], end
            return {"type": "http.request", "body": part, "more_body": end < len(body)}
        if not cancel and (child_probe["root"] / "ready").exists():
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        output.append(message)

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": CREATE, "raw_path": CREATE.encode(), "query_string": b"",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1234),
        "headers": [(key.lower(), value) for key, value in outgoing.headers.raw],
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        async with asyncio.timeout(10):
            while not (child_probe["root"] / "ready").exists():
                if task.done():
                    task.result()
                    pytest.fail(f"Request ended before metadata started: {output!r}")
                await asyncio.sleep(.01)
        # FastAPI has parsed the replayed multipart; auth spools are already
        # closed while the private candidate/real child remain active.
        assert authentication and all(spool.closed for spool in authentication)
        assert not manager._sessions
        assert list(session.SESSION_ROOT.glob("*/source.pdf"))
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            await asyncio.wait_for(task, 10)
            assert next(m["status"] for m in output if m["type"] == "http.response.start") == 499
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert_probe_reaped(child_probe)
    assert_clean(manager, tmp_path)
    child_probe["mode"] = "valid"
    async with client() as http:
        outgoing, _ = await signed_request(http, data=small_pdf())
        assert (await http.send(outgoing)).status_code == 200

"""Synthetic render-session metadata boundary regressions; no external services."""
import asyncio
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pymupdf as fitz
import pytest
from fastapi import FastAPI, HTTPException, UploadFile
import httpx

from app.api.tools.render import limiter, renderer, router, service, session, session_metadata as metadata


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    instance = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", instance)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(metadata.tempfile, "tempdir", str(temporary))
    admission = limiter.RenderConcurrencyLimiter(max_concurrency=2, max_queue=2, render_timeout=10, queue_timeout=1)
    monkeypatch.setattr(limiter, "render_limiter", admission)
    yield instance
    for identifier in list(instance._sessions):
        instance.delete(identifier)


async def create(data):
    upload = UploadFile(filename="small.pdf", file=io.BytesIO(data))
    try:
        return await service.create_render_session(upload)
    finally:
        await upload.close()


def assert_clean(manager, tmp_path):
    assert not manager._sessions
    assert not list(session.SESSION_ROOT.iterdir())
    # Existing native capacity keeps one empty, unlocked lock inode.
    assert all(p.name.startswith("platen-native-slots-") for p in (tmp_path / "temporary").iterdir())
    for fd in Path("/proc/self/fd").iterdir():
        try:
            assert not os.readlink(fd).startswith(str(tmp_path))
        except FileNotFoundError:
            pass


def assert_dead(pid):
    path = Path(f"/proc/{pid}/stat")
    # SIGKILL delivery is asynchronous. Wait for the same exit predicate rather
    # than treating the signal syscall's return as acknowledgment of death.
    deadline = time.monotonic() + 2
    while True:
        try:
            state = path.read_text().split()[2]
        except FileNotFoundError:
            return
        if state == "Z":
            return
        assert time.monotonic() < deadline, f"fixture descendant {pid} remains live ({state})"
        time.sleep(.01)


async def wait_for_marker(path):
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.fixture
def child_probe(tmp_path, monkeypatch):
    from app.core import subprocess_runner
    actual = subprocess_runner.run_hardened_subprocess
    original_popen = subprocess.Popen
    root = tmp_path / "probe"
    root.mkdir()
    state = {"mode": "valid", "root": root, "processes": []}

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        state["processes"].append(process)
        return process

    def launch(command, **kwargs):
        child = [sys.executable, "-m", "tests.render_metadata_probes", str(root), state["mode"]]
        guardian = "from app.core.native_supervisor import main;import sys;sys.exit(main(" + repr(child) + "))"
        return actual([sys.executable, "-c", guardian, command[3], str(os.getpid())], **kwargs)

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess_runner, "run_hardened_subprocess", launch)
    yield state
    for process in state["processes"]:
        if process.poll() is None:
            os.killpg(process.pid, 9)
        process.wait(timeout=5)


def assert_probe_reaped(probe):
    for name in ("child.pid", "grandchild.pid"):
        marker = probe["root"] / name
        if marker.exists():
            assert_dead(int(marker.read_text()))
    for process in probe["processes"]:
        assert process.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)


def small_pdf(pages=1):
    with fitz.open() as document:
        for number in range(pages):
            page = document.new_page(width=200, height=100)
            page.insert_text((20, 30), f"Metadata page {number + 1}")
        return document.tobytes()


@pytest.mark.anyio
async def test_native_parse_never_runs_in_api(manager, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Native PDF metadata parsing ran in API process")
    monkeypatch.setattr(renderer.PdfRenderDocument, "open", forbidden)
    upload = UploadFile(filename="small.pdf", file=io.BytesIO(small_pdf(2)))
    try:
        created = await service.create_render_session(upload)
        assert created.page_count == 2
        assert created.file_path.exists()
        assert created.document is None
    finally:
        await upload.close()


@pytest.mark.anyio
@pytest.mark.parametrize("pages", [1, 3])
async def test_real_metadata_dedup_and_page_retrieval(manager, pages):
    from PIL import Image
    data = small_pdf(pages)
    first = await create(data)
    again = await create(data)
    assert first.session_id == again.session_id
    assert first.page_count == pages
    assert first.file_size == len(data)
    assert first.file_path.read_bytes() == data
    assert first.file_path.stat().st_mode & 0o777 == 0o600
    result = await service.get_render_session_page(first.session_id, pages, 72)
    assert Image.open(io.BytesIO(result)).format == "JPEG"
    assert len(manager._sessions) == 1


def zero_page_pdf():
    # Small valid PDF catalog and an empty page tree; PyMuPDF cannot save an
    # empty new Document directly. No compressed or extreme geometry fixture.
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(output)
    output.extend(b"xref\n0 3\n0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@pytest.mark.anyio
@pytest.mark.parametrize("data,message", [(b"", "Empty file"), (b"not pdf", "valid PDF"),
                                           (b"%PDF-1.4\nbroken", "valid PDF"), (zero_page_pdf(), "no pages")])
async def test_invalid_empty_and_corrupt_publish_nothing(manager, tmp_path, data, message):
    with pytest.raises(ValueError, match=message):
        await create(data)
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_failed_candidate_does_not_evict_valid_session(manager, monkeypatch):
    monkeypatch.setattr(session, "MAX_SESSIONS", 1)
    retained = await create(small_pdf())
    with pytest.raises(ValueError):
        await create(b"invalid PDF")
    assert manager.get(retained.session_id) is retained
    assert retained.file_path.exists()
    assert len(list(session.SESSION_ROOT.iterdir())) == 1


@pytest.mark.anyio
async def test_timeout_reaps_tree_cleans_and_recovers(manager, child_probe, tmp_path, monkeypatch):
    child_probe["mode"] = "hang"
    monkeypatch.setattr(metadata, "METADATA_TIMEOUT_SECONDS", 3)
    with pytest.raises(metadata.MetadataTimeout):
        await create(small_pdf())
    assert (child_probe["root"] / "ready").exists()
    assert_probe_reaped(child_probe)
    assert_clean(manager, tmp_path)
    child_probe["mode"] = "valid"
    assert (await create(small_pdf())).page_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["crash", "crash_tree", "flood"])
async def test_failed_child_output_is_bounded_and_clean(manager, child_probe, tmp_path, mode):
    child_probe["mode"] = mode
    with pytest.raises(metadata.MetadataFailed):
        await create(small_pdf())
    assert_probe_reaped(child_probe)
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_cancel_and_repeat_cancel_join_cleanup(manager, child_probe, tmp_path, monkeypatch):
    from app.core import subprocess_runner
    child_probe["mode"] = "hang"
    entered, release = threading.Event(), threading.Event()
    actual = subprocess_runner.kill_process_group

    def stop(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(subprocess_runner, "kill_process_group", stop)
    task = asyncio.create_task(create(small_pdf()))
    try:
        await wait_for_marker(child_probe["root"] / "ready")
        task.cancel()
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert list(session.SESSION_ROOT.glob("*/source.pdf"))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert_probe_reaped(child_probe)
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_native_wait_does_not_hold_manager_lock_or_event_loop(manager, child_probe):
    retained = await create(small_pdf())
    child_probe["mode"] = "hang"
    task = asyncio.create_task(create(small_pdf(3)))
    try:
        await wait_for_marker(child_probe["root"] / "ready")
        def check_lock():
            acquired = manager._lock.acquire(blocking=False)
            if acquired:
                manager._lock.release()
            return acquired
        assert await asyncio.to_thread(check_lock)
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert (await client.get("/health/live")).json() == {"status": "alive"}
        manager.delete(retained.session_id)
        assert not retained.file_path.exists()
        assert not manager._sessions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert_probe_reaped(child_probe)


@pytest.mark.anyio
async def test_cancelled_copy_is_joined_before_removing_candidate(manager, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    actual_copy = session.shutil.copyfile
    inspected = False
    def blocked_copy(*args):
        entered.set()
        assert release.wait(10)
        return actual_copy(*args)
    async def inspect(path):
        nonlocal inspected
        inspected = True
        return 1
    monkeypatch.setattr(session.shutil, "copyfile", blocked_copy)
    monkeypatch.setattr(session, "inspect_page_count", inspect)
    task = asyncio.create_task(create(small_pdf()))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        assert list(session.SESSION_ROOT.iterdir())
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not inspected
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_session_limit_eviction_and_expiry_after_verified_publication(manager, monkeypatch):
    monkeypatch.setattr(session, "MAX_SESSIONS", 1)
    first = await create(small_pdf())
    second = await create(small_pdf(2))
    assert not first.file_path.exists()
    assert second.file_path.exists()
    assert len(manager._sessions) == 1
    second.last_accessed_at = 0
    assert manager.get_optional(second.session_id) is None
    assert not second.file_path.exists()


@pytest.mark.anyio
async def test_api_creation_queue_is_bounded_before_staging(manager, monkeypatch):
    admission = limiter.RenderConcurrencyLimiter(max_concurrency=1, max_queue=1, queue_timeout=5, render_timeout=10)
    monkeypatch.setattr(limiter, "render_limiter", admission)
    entered, release = asyncio.Event(), asyncio.Event()
    async def inspect(path):
        entered.set()
        await release.wait()
        return 1
    monkeypatch.setattr(session, "inspect_page_count", inspect)
    first = asyncio.create_task(create(small_pdf()))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(create(small_pdf(2)))
        async with asyncio.timeout(5):
            while admission.queued_renders != 1:
                await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            await create(small_pdf(3))
        assert error.value.status_code == 429
        assert error.value.detail["code"] == "RENDER_QUEUE_FULL"
        assert len(list(session.SESSION_ROOT.iterdir())) == 1
        assert admission.active_renders == 1
    finally:
        first.cancel()
        if second is not None:
            second.cancel()
        release.set()
        await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
    assert admission.active_renders == admission.queued_renders == 0


@pytest.mark.anyio
async def test_disconnect_cancels_real_metadata_child(manager, child_probe, tmp_path):
    child_probe["mode"] = "hang"
    class Request:
        async def is_disconnected(self):
            return (child_probe["root"] / "ready").exists()
    upload = UploadFile(filename="small.pdf", file=io.BytesIO(small_pdf()))
    try:
        with pytest.raises(HTTPException) as error:
            await router.create_session(Request(), upload)
        assert error.value.status_code == 499
    finally:
        await upload.close()
    assert_probe_reaped(child_probe)
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_native_capacity_busy_is_distinct(manager, tmp_path):
    from app.core.native_isolation import _capacity
    with _capacity(lambda: None, 0):
        with pytest.raises(metadata.MetadataBusy):
            await create(small_pdf())
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_duplicate_publication_is_atomic(manager, monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def inspect(path):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        return 1
    monkeypatch.setattr(session, "inspect_page_count", inspect)
    data = small_pdf()
    path1 = tmp_path / "cand1.pdf"
    path1.write_bytes(data)
    path2 = tmp_path / "cand2.pdf"
    path2.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    tasks = [
        asyncio.create_task(manager.create(path1, len(data), sha, "global")),
        asyncio.create_task(manager.create(path2, len(data), sha, "global")),
    ]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not manager._sessions
        release.set()
        first, second = await asyncio.gather(*tasks)
        assert first is second
        assert first.file_path.exists()
        assert len(list(session.SESSION_ROOT.iterdir())) == 1
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("evict", [False, True])
async def test_deletion_or_eviction_cannot_remove_unpublished_candidate(manager, monkeypatch, tmp_path, evict):
    monkeypatch.setattr(session, "MAX_SESSIONS", 1)
    data = small_pdf()
    retained = await create(data)
    entered, release = asyncio.Event(), asyncio.Event()
    async def inspect(path):
        entered.set()
        await release.wait()
        assert path.exists()
        return 1
    monkeypatch.setattr(session, "inspect_page_count", inspect)
    source = tmp_path / "candidate.pdf"
    source.write_bytes(data)
    pending = asyncio.create_task(manager.create(source, len(data), retained.sha256))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if evict:
            with manager._lock:
                manager._evict_oldest_locked()
        else:
            manager.delete(retained.session_id)
        assert not retained.file_path.exists()
        release.set()
        result = await pending
        assert result.session_id != retained.session_id
        assert result.file_path.read_bytes() == data
        assert manager.get(result.session_id) is result
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.anyio
async def test_copy_failure_and_read_cancellation_close_staging(manager, monkeypatch, tmp_path):
    def fail_copy(*args):
        raise OSError("synthetic private path failure")
    with monkeypatch.context() as patch:
        patch.setattr(session.shutil, "copyfile", fail_copy)
        with pytest.raises(OSError):
            await create(small_pdf())
    assert_clean(manager, tmp_path)
    class CancelledUpload:
        async def read(self, size):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.create_render_session(CancelledUpload())
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
async def test_staging_close_failure_still_removes_upload(manager, monkeypatch, tmp_path):
    actual = service.tempfile.NamedTemporaryFile
    class FailingFile:
        def __init__(self, *args, **kwargs):
            self.file = actual(*args, **kwargs)
            self.name = self.file.name
        def write(self, data):
            return self.file.write(data)
        def flush(self):
            raise OSError("synthetic flush failure")
        def close(self):
            self.file.close()
            raise OSError("synthetic close failure")
    monkeypatch.setattr(service.tempfile, "NamedTemporaryFile", FailingFile)
    with pytest.raises(OSError):
        await create(small_pdf())
    assert_clean(manager, tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("failure,status,code", [
    (ValueError("/private/source.pdf"), 400, "INVALID_DOCUMENT"),
    (metadata.MetadataBusy(), 429, "SESSION_METADATA_BUSY"),
    (metadata.MetadataTimeout(), 504, "SESSION_METADATA_TIMEOUT"),
    (metadata.MetadataFailed(), 503, "SESSION_METADATA_FAILED"),
    (metadata.MetadataUnavailable(), 503, "SESSION_METADATA_UNAVAILABLE"),
    (RuntimeError("/private/source.pdf"), 500, "SESSION_CREATE_FAILED"),
])
async def test_safe_http_errors(manager, monkeypatch, failure, status, code):
    async def fail(path):
        raise failure
    monkeypatch.setattr(session, "inspect_page_count", fail)
    local = FastAPI()
    local.include_router(router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local), base_url="http://127.0.0.1") as client:
        response = await client.post("/api/v1/render/sessions", files={"file": ("small.pdf", small_pdf(), "application/pdf")})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert "/private" not in response.text
    assert not manager._sessions


@pytest.mark.anyio
async def test_http_success_shape_unchanged(manager):
    local = FastAPI()
    local.include_router(router.router)
    data = small_pdf(2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local), base_url="http://127.0.0.1") as client:
        response = await client.post("/api/v1/render/sessions", files={"file": ("small.pdf", data, "application/pdf")})
    assert response.status_code == 200
    assert set(response.json()) == {"session_id", "page_count", "file_size"}
    assert response.json()["page_count"] == 2
    assert response.json()["file_size"] == len(data)

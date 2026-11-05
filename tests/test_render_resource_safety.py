import asyncio
import io
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf as fitz
import pytest
from PIL import Image

from app.api.tools.render import renderer, service, session
from app.api.tools.render.limiter import render_limiter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize('dpi', [0, -1, float('nan'), float('inf'), 601])
def test_invalid_dpi_before_native(dpi):
    doc = Mock()
    doc.page_count = 1
    with pytest.raises(ValueError):
        renderer.render_page_from_document(doc, 1, dpi)
    doc.__getitem__.assert_not_called() if hasattr(doc, '__getitem__') else None


def test_extreme_geometry_rejected_before_allocation():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 10000, 10000), rotation=0, get_pixmap=Mock())
    class Document:
        page_count = 1
        def __getitem__(self, index): return page
    with pytest.raises(ValueError):
        renderer.render_page_from_document(Document(), 1, 144)
    page.get_pixmap.assert_not_called()


def pdf():
    with fitz.open() as doc:
        page = doc.new_page(width=200, height=100)
        page.insert_text((20, 30), 'F12 preview')
        page.set_rotation(90)
        return doc.tobytes()


def test_rotation_fractional_dpi_and_clip():
    result = renderer.render_pdf_page_to_jpeg(pdf(), 1, 108.5, 0, 0, 50, 100)
    image = Image.open(io.BytesIO(result))
    assert 74 <= image.width <= 77
    assert 149 <= image.height <= 153


@pytest.mark.parametrize('clip', [(0, None, 10, 20), (0, 0, float('inf'), 20), (20, 0, 10, 20), (1000,1000,1010,1010)])
def test_bad_clip(clip):
    with pytest.raises(ValueError):
        renderer.render_pdf_page_to_jpeg(pdf(), 1, 144, *clip)


@pytest.mark.anyio
async def test_session_subprocess_no_cache_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(session, 'SESSION_ROOT', tmp_path / 'sessions')
    session.SESSION_ROOT.mkdir()
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, 'session_manager', manager)
    path = tmp_path / 'sample.pdf'
    path.write_bytes(pdf())
    item = await manager.create(path, path.stat().st_size, 'fixture')
    assert item.document is None
    for dpi in (72, 108.5, 144):
        result = await service.get_render_session_page(item.session_id, 1, dpi)
        assert Image.open(io.BytesIO(result)).format == 'JPEG'
    assert not hasattr(item, 'page_cache')
    manager.delete(item.session_id)
    assert not item.file_path.exists()
    with pytest.raises(KeyError): manager.get(item.session_id)
    item = await manager.create(path, path.stat().st_size, 'fixture')
    item.last_accessed_at = 0
    with pytest.raises(KeyError): manager.get(item.session_id)
    assert not item.file_path.exists()


@pytest.mark.anyio
async def test_native_timeout_kills_and_next_render_succeeds(monkeypatch):
    import sys
    original = asyncio.create_subprocess_exec
    children = []
    async def stuck(*args, **kwargs):
        child = await original(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        children.append(child)
        return child
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, 'create_subprocess_exec', stuck)
        patch.setattr(render_limiter, 'render_timeout', .05)
        with pytest.raises(asyncio.TimeoutError):
            await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert children[0].returncode is not None
    result, _ = await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert Image.open(io.BytesIO(result)).format == 'JPEG'


@pytest.mark.anyio
async def test_subprocess_drains_both_pipes_above_capacity_with_bounded_capture(monkeypatch):
    import sys
    original = asyncio.create_subprocess_exec

    async def noisy(*command, **kwargs):
        output = command[command.index("--output") + 1]
        script = (
            "import os,sys; open(sys.argv[1],'wb').write(b'jpeg'); "
            "os.write(1,b'{\\\"ok\\\":true}\\n'+b'x'*300000); "
            "os.write(2,b'e'*300000)"
        )
        return await original(sys.executable, "-c", script, output, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", noisy)
    rendered, metrics = await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert rendered == b"jpeg"
    assert metrics == {}, "oversized child telemetry is drained but not retained or parsed"


@pytest.mark.anyio
async def test_timeout_kills_descendant_after_child_exits_with_pipe_open(monkeypatch, tmp_path):
    import os
    import sys
    original = asyncio.create_subprocess_exec
    pid_file = tmp_path / "descendant.pid"
    children = []

    async def exits_with_descendant(*_command, **kwargs):
        script = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid)); print('leader-exit',flush=True)"
        )
        child = await original(sys.executable, "-c", script, str(pid_file), **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exits_with_descendant)
    monkeypatch.setattr(render_limiter, "render_timeout", 0.1)
    with pytest.raises(asyncio.TimeoutError):
        await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)

    assert children[0].returncode is not None
    descendant = int(pid_file.read_text())
    try:
        os.kill(descendant, 0)
    except ProcessLookupError:
        return
    try:
        # SIGKILL delivery is asynchronous. Wait for exit or zombie state
        # rather than racing the kernel immediately after the render returns.
        deadline = time.monotonic() + 2
        while True:
            try:
                stat = open(f"/proc/{descendant}/stat", encoding="utf-8").read().split()
            except FileNotFoundError:
                break
            if stat[2] == "Z":
                break
            assert time.monotonic() < deadline, "the process-group descendant must be dead, not running"
            time.sleep(0.01)
    finally:
        try:
            os.kill(descendant, 9)
        except ProcessLookupError:
            pass


@pytest.mark.anyio
async def test_success_kills_descendant_that_closed_capture_pipes(monkeypatch, tmp_path):
    import os
    import sys
    original = asyncio.create_subprocess_exec
    pid_file = tmp_path / "success-descendant.pid"
    children = []

    async def exits_with_detached_pipe_descendant(*_command, **kwargs):
        output = _command[_command.index("--output") + 1]
        script = (
            "import os,subprocess,sys; open(sys.argv[1],'wb').write(b'jpeg'); "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "open(sys.argv[2],'w').write(str(p.pid))"
        )
        child = await original(sys.executable, "-c", script, output, str(pid_file), **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exits_with_detached_pipe_descendant)
    rendered, _ = await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert rendered == b"jpeg"
    assert children[0].returncode is not None
    descendant = int(pid_file.read_text())
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        try:
            stat = open(f"/proc/{descendant}/stat", encoding="utf-8").read().split()
            if stat[2] == "Z":
                return
        except FileNotFoundError:
            return
        await asyncio.sleep(0.01)
    try:
        os.kill(descendant, 9)
    except ProcessLookupError:
        pass
    pytest.fail("a successful render must not leave its process-group descendant running")


def test_render_child_hard_limits_are_installed_and_unavailable_mode_fails_closed():
    from types import SimpleNamespace
    from app.api.tools.render import child_limits

    class FakeResource:
        RLIM_INFINITY = -1
        RLIMIT_AS = 1
        RLIMIT_CPU = 2
        RLIMIT_FSIZE = 3

        def __init__(self):
            self.limits = {1: (-1, -1), 2: (-1, -1), 3: (-1, -1)}

        def getrlimit(self, kind):
            return self.limits[kind]

        def setrlimit(self, kind, value):
            self.limits[kind] = value

    resource_api = FakeResource()
    child_limits.apply_render_child_limits(resource_api)
    assert resource_api.limits[resource_api.RLIMIT_AS] == (
        child_limits.MAX_RENDER_CHILD_ADDRESS_SPACE,
        child_limits.MAX_RENDER_CHILD_ADDRESS_SPACE,
    )
    assert resource_api.limits[resource_api.RLIMIT_CPU] == (
        child_limits.MAX_RENDER_CHILD_CPU_SECONDS,
        child_limits.MAX_RENDER_CHILD_CPU_SECONDS,
    )
    assert resource_api.limits[resource_api.RLIMIT_FSIZE] == (
        child_limits.MAX_RENDER_CHILD_FILE_BYTES,
        child_limits.MAX_RENDER_CHILD_FILE_BYTES,
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        child_limits.apply_render_child_limits(SimpleNamespace())


def test_render_concurrency_and_timeout_overrides_cannot_exceed_hard_caps(monkeypatch):
    from app.api.tools.render import limiter

    monkeypatch.setenv("RENDER_CONCURRENCY_LIMIT", "99")
    assert limiter.get_default_concurrency_limit() == limiter.HARD_MAX_RENDER_CONCURRENCY
    instance = limiter.RenderConcurrencyLimiter(max_concurrency=99, render_timeout=999)
    assert instance.max_concurrency == limiter.HARD_MAX_RENDER_CONCURRENCY
    assert instance.render_timeout == limiter.HARD_MAX_RENDER_TIMEOUT_SECONDS


@pytest.mark.anyio
async def test_disconnect_cancels_and_reaps_operation():
    from app.api.tools.render.router import render_until_disconnect
    from fastapi import HTTPException
    cleaned = asyncio.Event()
    async def operation():
        try: await asyncio.sleep(60)
        finally: cleaned.set()
    class Request:
        async def is_disconnected(self): return True
    with pytest.raises(HTTPException) as error:
        await render_until_disconnect(Request(), operation())
    assert error.value.status_code == 499
    assert cleaned.is_set()


@pytest.mark.anyio
async def test_cancelled_pool_render_kills_worker_and_recovers(monkeypatch):
    from app.api.tools.render.persistent_pool import PersistentRenderWorkerPool
    monkeypatch.setenv('ENABLE_RENDER_FAILURE_INJECTION', 'true')
    monkeypatch.setenv('APP_ENV', 'test')
    pool = PersistentRenderWorkerPool(size=1)
    await pool.start()
    worker = pool.workers[1]
    proc = worker.proc
    task = asyncio.create_task(pool.render(pdf(), 1, 72, simulate_hang=True))
    try:
        while not pool.available_workers.empty():
            await asyncio.sleep(.01)
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
        assert pool.available_workers.qsize() == 1
        result, _ = await pool.render(pdf(), 1, 72)
        assert Image.open(io.BytesIO(result)).format == 'JPEG'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.shutdown()

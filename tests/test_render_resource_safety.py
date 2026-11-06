from tests.render_guardian_probes import patch_render_child
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
    with monkeypatch.context() as patch:
        children = patch_render_child(patch, lambda command: [sys.executable, '-c', 'import time;time.sleep(60)'])
        patch.setattr(render_limiter, 'render_timeout', .2)
        with pytest.raises(asyncio.TimeoutError):
            await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert children[0].returncode is not None
    result, _ = await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert Image.open(io.BytesIO(result)).format == 'JPEG'


@pytest.mark.anyio
async def test_subprocess_drains_both_pipes_above_capacity_with_bounded_capture(monkeypatch):
    import sys
    def native(command):
        output = command[command.index('--output') + 1]
        script = "import os,sys;open(sys.argv[1],'wb').write(b'jpeg');os.write(1,b'x'*300000);os.write(2,b'e'*300000)"
        return [sys.executable, '-c', script, output]
    patch_render_child(monkeypatch, native)
    rendered, metrics = await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert rendered == b'jpeg'
    assert metrics == {}


@pytest.mark.anyio
async def test_exited_child_descendant_is_reaped_without_capture_timeout(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    marker = tmp_path / 'descendant.pid'
    script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open(sys.argv[1],'w').write(str(p.pid))"
    children = patch_render_child(monkeypatch, lambda command: [sys.executable, '-c', script, str(marker)])
    monkeypatch.setattr(render_limiter, 'render_timeout', 2)
    with pytest.raises(RuntimeError, match='did not generate output'):
        await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)
    assert children[0].returncode is not None
    assert not Path(f'/proc/{int(marker.read_text())}').exists(), 'guardian must reap its adopted descendant'


@pytest.mark.anyio
async def test_success_kills_descendant_that_closed_capture_pipes(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    marker = tmp_path / 'descendant.pid'
    def native(command):
        output = command[command.index('--output') + 1]
        script = "import subprocess,sys;open(sys.argv[1],'wb').write(b'jpeg');p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);open(sys.argv[2],'w').write(str(p.pid))"
        return [sys.executable, '-c', script, output, str(marker)]
    children = patch_render_child(monkeypatch, native)
    assert (await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72))[0] == b'jpeg'
    assert children[0].returncode == 0
    assert not Path(f'/proc/{int(marker.read_text())}').exists()


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


@pytest.mark.anyio
async def test_direct_upload_rejects_oversize_before_starting_child(monkeypatch):
    from app.api.tools.render import service
    from fastapi import HTTPException
    monkeypatch.setattr(service, "MAX_RENDER_INPUT_BYTES", 8, raising=False)
    calls = []
    class Upload:
        async def read(self, size=-1):
            assert size > 0, "uploads must never use an unbounded read"
            calls.append(size)
            return b"%PDF" + b"x" * 5
    async def forbidden_child(**_kwargs):
        pytest.fail("oversized upload reached native child")
    monkeypatch.setattr(service, "render_page_to_jpeg_bytes_subprocess", forbidden_child)
    with pytest.raises(HTTPException) as error:
        await service.render_page_to_jpeg_bytes(Upload(), 1, 72)
    assert error.value.status_code == 413
    assert calls == [9]


@pytest.mark.anyio
async def test_subprocess_rejects_large_output_before_reading_into_api_memory(monkeypatch):
    import sys
    monkeypatch.setattr(service, 'MAX_RENDER_OUTPUT_BYTES', 8)
    def native(command):
        return [sys.executable, '-c', "import sys;open(sys.argv[1],'wb').write(b'x'*9)", command[command.index('--output')+1]]
    patch_render_child(monkeypatch, native)
    with pytest.raises(RuntimeError, match='output.*limit'):
        await service.render_page_to_jpeg_bytes_subprocess(pdf(), 1, 72)


def test_render_guardian_owner_death_kills_native_tree(tmp_path):
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    child_marker = tmp_path / "native.pid"
    guardian_marker = tmp_path / "guardian.pid"
    native = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    guardian = (
        "from app.api.tools.render.supervisor import supervise; import os,sys; "
        f"sys.exit(supervise([{sys.executable!r},'-c',{native!r}], owner_pid=os.getppid(), root_lock_fd=None, cancel_fd=None, wall_timeout=30))"
    )
    owner_script = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([{sys.executable!r},'-c',{guardian!r}],start_new_session=True); "
        f"Path({str(guardian_marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    def dead(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except FileNotFoundError:
            return True
    try:
        deadline = time.monotonic() + 10
        while not child_marker.exists():
            assert owner.poll() is None
            assert time.monotonic() < deadline, "supervised child did not start"
            time.sleep(.01)
        guardian_pid = int(guardian_marker.read_text())
        child_pid = int(child_marker.read_text())
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 3
        while not (dead(guardian_pid) and dead(child_pid)):
            assert time.monotonic() < deadline, "render owner death left its native tree alive"
            time.sleep(.01)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if guardian_marker.exists():
            try:
                os.killpg(int(guardian_marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_real_render_child_file_ceiling_prevents_sparse_oversize(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "oversized.bin"
    script = (
        "import errno,resource,sys; "
        "from app.api.tools.render.child_limits import apply_render_child_limits,MAX_RENDER_CHILD_FILE_BYTES; "
        "apply_render_child_limits(); "
        "f=open(sys.argv[1],'wb'); f.seek(MAX_RENDER_CHILD_FILE_BYTES); "
        "\ntry:\n f.write(b'x'); f.flush()\n"
        "except OSError as e:\n assert e.errno == errno.EFBIG\n"
        "else:\n raise AssertionError('file-size limit did not reject write')\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    from app.api.tools.render.child_limits import MAX_RENDER_CHILD_FILE_BYTES
    assert path.stat().st_size <= MAX_RENDER_CHILD_FILE_BYTES


@pytest.mark.anyio
async def test_repeated_cancellation_during_spawn_joins_and_reaps_before_return(monkeypatch, tmp_path):
    import threading
    import sys
    from app.core import subprocess_runner
    from app.api.tools.render import session
    root = tmp_path / 'sessions'; root.mkdir()
    monkeypatch.setattr(session, 'SESSION_ROOT', root)
    manager = session.RenderSessionManager(); monkeypatch.setattr(service, 'session_manager', manager)
    children = patch_render_child(monkeypatch, lambda command: [sys.executable, '-c', 'import time;time.sleep(60)'])
    original = subprocess_runner.subprocess.Popen
    started, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        child = original(*args, **kwargs)
        started.set()
        assert release.wait(5)
        return child
    monkeypatch.setattr(subprocess_runner.subprocess, 'Popen', delayed)
    task = asyncio.create_task(service.render_page_to_jpeg_bytes_subprocess(b'%PDF', 1, 72))
    try:
        async with asyncio.timeout(5):
            while not started.is_set(): await asyncio.sleep(.01)
        task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert children[0].returncode is not None
        assert manager.used_bytes == 0
        assert list(root.iterdir()) == []
    finally:
        release.set(); task.cancel(); await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["page", "metadata"])
def test_stopped_guardian_cancellation_is_bounded_and_quarantines_storage(tmp_path, operation):
    import os
    import subprocess
    import sys
    root = tmp_path / "sessions"
    script = r'''
import asyncio, os, signal, sys, time
from pathlib import Path
from app.api.tools.render import service, session, session_metadata
from app.core import subprocess_runner
original = subprocess_runner.subprocess.Popen
children = []
def stop_guardian(*args, **kwargs):
    process = original(*args, **kwargs)
    children.append(process)
    os.kill(process.pid, signal.SIGSTOP)
    return process
subprocess_runner.subprocess.Popen = stop_guardian
subprocess_runner.os.killpg = lambda *args: (_ for _ in ()).throw(AssertionError('numeric PGID signaling used'))
async def run():
    if sys.argv[1] == 'page':
        operation = service.render_page_to_jpeg_bytes_subprocess(b'%PDF-synthetic', 1, 72)
    else:
        source = session.SESSION_ROOT / 'pdfnest-session-upload-stopped.pdf'
        source.write_bytes(b'%PDF-synthetic')
        operation = session.session_manager.create(source, source.stat().st_size, 'stopped', 'owner')
    task = asyncio.create_task(operation)
    async with asyncio.timeout(3):
        while not children: await asyncio.sleep(.01)
    started = time.monotonic()
    task.cancel()
    try:
        await task
    except (RuntimeError, asyncio.CancelledError):
        pass
    else:
        raise AssertionError('stopped guardian cancellation falsely succeeded')
    assert time.monotonic() - started < 4, 'stopped guardian held cancellation open'
    assert children[0].returncode is not None
    assert session._SESSION_ROOT_QUARANTINED
    assert session.session_manager._reserved_bytes + session.session_manager._committed_bytes > 0
    assert list(session.SESSION_ROOT.rglob('*.pdf')), 'unverified source bytes were deleted'
    try: session.session_manager.reserve_storage('owner')
    except RuntimeError: pass
    else: raise AssertionError('quarantined root admitted another writer')
asyncio.run(run())
'''
    env = {**os.environ, "PDFNEST_RENDER_SESSION_DIR": str(root), "APP_ENV": "test"}
    result = subprocess.run([sys.executable, "-c", script, operation], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    restarted = subprocess.run([sys.executable, "-c", "import app.api.tools.render.session"],
                               env=env, capture_output=True, text=True, timeout=10)
    assert restarted.returncode != 0
    assert "drainage is unverified" in restarted.stderr

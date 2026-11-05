from __future__ import annotations

from tests.render_guardian_probes import patch_render_child

import asyncio
import hashlib
import threading
import time
from pathlib import Path

import fitz
import pytest

from app.api.tools.render import service, session


def test_session_root_claim_cleans_stale_artifacts_and_rejects_second_manager(tmp_path: Path):
    root = tmp_path / "sessions"
    session._claim_session_root(root).close()
    stale_session = root / "0123456789abcdef0123456789abcdef"
    stale_session.mkdir(parents=True)
    (stale_session / "source.pdf").write_bytes(b"synthetic stale data")
    stale_upload = root / "pdfnest-session-upload-orphan.pdf"
    stale_upload.write_bytes(b"synthetic partial upload")

    lock = session._claim_session_root(root)
    try:
        assert set(root.iterdir()) == {root / ".manager.lock", root / session.ROOT_PROTOCOL_MARKER}
        with pytest.raises(RuntimeError, match="already owned"):
            session._claim_session_root(root)
    finally:
        lock.close()


def test_session_root_restart_fails_closed_when_orphan_cleanup_fails(tmp_path: Path, monkeypatch):
    root = tmp_path / "sessions"
    session._claim_session_root(root).close()
    orphan = root / "0123456789abcdef0123456789abcdef"
    orphan.mkdir(parents=True)
    (orphan / "source.pdf").write_bytes(b"synthetic orphan")

    def fail_remove(path, **_kwargs):
        raise OSError("synthetic filesystem denial")

    monkeypatch.setattr(session.shutil, "rmtree", fail_remove)
    with pytest.raises(OSError, match="synthetic filesystem denial"):
        session._claim_session_root(root)

    assert (orphan / "source.pdf").read_bytes() == b"synthetic orphan"
    # A failed startup reconciliation must not leave a lock that prevents a
    # later process from retrying cleanup.
    monkeypatch.undo()
    lock = session._claim_session_root(root)
    try:
        assert not orphan.exists()
        assert set(root.iterdir()) == {root / ".manager.lock", root / session.ROOT_PROTOCOL_MARKER}
    finally:
        lock.close()


def test_aggregate_and_owner_reservations_are_atomic(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(session, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 10)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 6)
    monkeypatch.setattr(session, "MAX_SESSION_FILE_BYTES", 6)
    manager = session.RenderSessionManager()

    barrier = threading.Barrier(12)
    accepted = []
    lock = threading.Lock()

    def reserve(index: int) -> None:
        barrier.wait()
        reservation = manager.reserve_storage(f"owner-{index}")
        try:
            reservation.extend(1)
        except session.SessionStorageLimitError:
            reservation.release()
            return
        with lock:
            accepted.append(reservation)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 10
    assert manager.used_bytes == 10
    for reservation in accepted:
        reservation.release()
    assert manager.used_bytes == 0


def test_per_owner_and_per_session_limits_fail_before_more_bytes_are_written(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(session, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 20)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 5)
    monkeypatch.setattr(session, "MAX_SESSION_FILE_BYTES", 4)
    manager = session.RenderSessionManager()

    first = manager.reserve_storage("owner-a")
    first.extend(4)
    with pytest.raises(session.SessionStorageLimitError):
        first.extend(1)

    second = manager.reserve_storage("owner-a")
    with pytest.raises(session.SessionStorageLimitError):
        second.extend(2)
    second.release()
    assert manager.used_bytes == 4
    first.release()
    assert manager.used_bytes == 0


@pytest.mark.anyio
async def test_published_session_budget_is_released_on_delete(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(session, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 1024 * 1024)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 1024 * 1024)
    monkeypatch.setattr(session, "MAX_SESSION_FILE_BYTES", 1024 * 1024)
    manager = session.RenderSessionManager()
    source = tmp_path / "source.pdf"
    document = fitz.open()
    document.new_page(width=612, height=792)
    document.save(source)
    document.close()
    reservation = manager.reserve_storage("owner-a")
    reservation.extend(source.stat().st_size)

    created = await manager.get_or_create(
        source,
        source.stat().st_size,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "owner-a",
        reservation=reservation,
    )

    assert manager.used_bytes == created.file_size
    assert created.file_path.is_file()
    manager.delete(created.session_id)
    assert manager.used_bytes == 0
    assert not created.file_path.exists()


def _register_synthetic_session(manager, root: Path, session_id: str, owner: str, payload: bytes):
    directory = root / session_id
    directory.mkdir()
    path = directory / "source.pdf"
    path.write_bytes(payload)
    now = time.time()
    created = session.RenderSession(
        session_id=session_id,
        file_path=path,
        file_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        page_count=1,
        created_at=now,
        last_accessed_at=now,
        dedup_scope=owner,
    )
    manager._sessions[session_id] = created
    manager._committed_bytes += len(payload)
    manager._committed_by_scope[owner] = manager._committed_by_scope.get(owner, 0) + len(payload)
    return created


def test_failed_delete_keeps_bytes_accounted_and_session_inaccessible(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 1024)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 1024)
    manager = session.RenderSessionManager()
    created = _register_synthetic_session(manager, root, "a" * 32, "owner-a", b"synthetic pdf bytes")

    def fail_remove(path, **_kwargs):
        assert Path(path) == created.file_path.parent
        raise OSError("synthetic filesystem denial")

    monkeypatch.setattr(session.shutil, "rmtree", fail_remove)
    manager.delete(created.session_id)

    assert created.file_path.exists()
    assert manager.used_bytes == created.file_size
    assert manager._committed_by_scope["owner-a"] == created.file_size
    assert manager.get_optional(created.session_id) is None
    with pytest.raises(KeyError):
        manager.get(created.session_id)


def test_pending_delete_blocks_concurrent_reservation_until_cleanup_outcome(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", len(b"synthetic pdf bytes"))
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 1024)
    manager = session.RenderSessionManager()
    created = _register_synthetic_session(manager, root, "b" * 32, "owner-a", b"synthetic pdf bytes")
    entered, release = threading.Event(), threading.Event()
    reservation_result = []

    def delay_then_fail(path, **_kwargs):
        assert Path(path) == created.file_path.parent
        entered.set()
        assert release.wait(5)
        raise OSError("synthetic filesystem denial")

    def delete():
        manager.delete(created.session_id)

    def reserve():
        reservation = manager.reserve_storage("owner-b")
        try:
            reservation.extend(1)
        except session.SessionStorageLimitError:
            reservation_result.append("rejected")
            reservation.release()
        else:
            reservation_result.append("admitted")

    monkeypatch.setattr(session.shutil, "rmtree", delay_then_fail)
    delete_thread = threading.Thread(target=delete)
    reserve_thread = threading.Thread(target=reserve)
    delete_thread.start()
    assert entered.wait(2)
    reserve_thread.start()
    try:
        time.sleep(0.05)
        assert not reservation_result
    finally:
        release.set()
    delete_thread.join(5)
    reserve_thread.join(5)

    assert not delete_thread.is_alive()
    assert not reserve_thread.is_alive()
    assert reservation_result == ["rejected"]
    assert manager.used_bytes == created.file_size
    assert created.file_path.exists()


def test_pending_delete_retries_and_releases_only_after_physical_removal(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    manager = session.RenderSessionManager()
    created = _register_synthetic_session(manager, root, "c" * 32, "owner-a", b"synthetic pdf bytes")
    another = _register_synthetic_session(manager, root, "d" * 32, "owner-b", b"other user bytes")
    actual_rmtree = session.shutil.rmtree
    attempts = 0

    def fail_once(path, **kwargs):
        nonlocal attempts
        if Path(path) == created.file_path.parent and attempts == 0:
            attempts += 1
            raise OSError("temporary filesystem denial")
        return actual_rmtree(path, **kwargs)

    monkeypatch.setattr(session.shutil, "rmtree", fail_once)
    manager.delete(created.session_id)
    assert manager.used_bytes == created.file_size + another.file_size
    assert created.file_path.exists()
    assert another.session_id in manager._sessions
    assert another.file_path.exists()

    manager.delete(created.session_id)
    assert attempts == 1
    assert not created.file_path.parent.exists()
    assert manager.used_bytes == another.file_size
    assert manager.get(another.session_id) is another


@pytest.mark.parametrize("cleanup", ["eviction", "expiry"])
def test_eviction_and_expiry_keep_failed_cleanup_accounted(monkeypatch, tmp_path: Path, cleanup: str):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 1024)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 1024)
    monkeypatch.setattr(session, "SESSION_TTL_SECONDS", 1)
    root.mkdir()
    manager = session.RenderSessionManager()
    created = _register_synthetic_session(manager, root, "e" * 32, "owner-a", b"synthetic pdf bytes")

    def fail_remove(path, **_kwargs):
        assert Path(path) == created.file_path.parent
        raise OSError("synthetic filesystem denial")

    with monkeypatch.context() as patch:
        patch.setattr(session.shutil, "rmtree", fail_remove)
        with manager._lock:
            if cleanup == "eviction":
                manager._evict_oldest_locked()
            else:
                created.last_accessed_at = 0
                manager._cleanup_expired_locked()
        assert manager.get_optional(created.session_id) is None
        assert manager.used_bytes == created.file_size
        assert created.file_path.exists()

    retry = manager.reserve_storage("owner-a")
    assert manager.used_bytes == 0
    retry.release()


@pytest.mark.anyio
async def test_failed_unpublished_candidate_remains_accounted_until_retry(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    payload = b"synthetic candidate bytes"
    source = tmp_path / "source.pdf"
    source.write_bytes(payload)
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", len(payload))
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", len(payload))

    async def fail_inspection(_path):
        raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr(session, "inspect_page_count", fail_inspection)

    def fail_remove(_path, **_kwargs):
        raise OSError("synthetic filesystem denial")

    manager = session.RenderSessionManager()
    with monkeypatch.context() as patch:
        patch.setattr(session.shutil, "rmtree", fail_remove)
        with pytest.raises(RuntimeError, match="synthetic metadata failure"):
            await manager.create(source, len(payload), hashlib.sha256(payload).hexdigest(), "owner-a")
        assert manager.used_bytes == len(payload)
        assert not manager._sessions
        assert len(manager._pending_deletions) == 1
        pending_path = next(iter(manager._pending_deletions))
        assert (pending_path / "source.pdf").read_bytes() == payload

    retry = manager.reserve_storage("owner-a")
    assert manager.used_bytes == 0
    assert not pending_path.exists()
    retry.release()


@pytest.mark.anyio
async def test_oversized_reservation_never_publishes_candidate(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic source")
    manager = session.RenderSessionManager()
    reservation = manager.reserve_storage("owner-a")
    reservation.extend(source.stat().st_size + 1)

    with pytest.raises(session.SessionStorageLimitError, match="exceeds the file size"):
        await manager.create(
            source,
            source.stat().st_size,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "owner-a",
            reservation=reservation,
        )

    assert not manager._sessions
    assert list(root.iterdir()) == []
    assert manager.used_bytes == source.stat().st_size + 1
    reservation.release()
    assert manager.used_bytes == 0


@pytest.mark.anyio
async def test_session_limit_never_evicts_another_owner(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    monkeypatch.setattr(session, "MAX_SESSIONS", 1)
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 1024)
    monkeypatch.setattr(session, "MAX_SESSION_OWNER_BYTES", 1024)
    monkeypatch.setattr(session, "MAX_SESSION_FILE_BYTES", 1024)
    manager = session.RenderSessionManager()
    existing = _register_synthetic_session(
        manager,
        root,
        "9" * 32,
        "owner-a",
        b"synthetic existing session",
    )
    candidate = tmp_path / "candidate.pdf"
    candidate.write_bytes(b"synthetic candidate session")

    async def inspect_one_page(_path):
        return 1

    monkeypatch.setattr(session, "inspect_page_count", inspect_one_page)

    with pytest.raises(session.SessionStorageLimitError, match="full for this owner"):
        await manager.create(
            candidate,
            candidate.stat().st_size,
            hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "owner-b",
        )

    assert manager.get(existing.session_id) is existing
    assert existing.file_path.read_bytes() == b"synthetic existing session"
    assert manager.used_bytes == existing.file_size
    assert manager._committed_by_scope == {"owner-a": existing.file_size}


@pytest.mark.anyio
async def test_dedup_hit_keeps_failed_staging_cleanup_accounted(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    manager = session.RenderSessionManager()
    payload = b"synthetic duplicate upload"
    existing = _register_synthetic_session(manager, root, "f" * 32, "owner-a", payload)
    staged = root / "pdfnest-session-upload-duplicate.pdf"
    staged.write_bytes(payload)
    reservation = manager.reserve_storage("owner-a")
    reservation.extend(len(payload))
    actual_unlink = Path.unlink
    actual_resolve = Path.resolve

    def fail_staging_unlink(path, *args, **kwargs):
        path = Path(path)
        if path == staged:
            raise OSError("synthetic filesystem denial")
        return actual_unlink(path, *args, **kwargs)

    def fail_stage_resolution(path, *args, **kwargs):
        path = Path(path)
        if path in (staged, root):
            raise OSError("synthetic path resolution failure")
        return actual_resolve(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_staging_unlink)
        patch.setattr(Path, "resolve", fail_stage_resolution)
        result = await manager.get_or_create(
            staged,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            "owner-a",
            reservation=reservation,
        )
        # The upload endpoint repeats cleanup in its finally block; retrying an
        # already-pending path must preserve, not drop, the original liability.
        manager.cleanup_upload_staging(staged, reservation, scope="owner-a", size_hint=len(payload))
        assert result is existing
        assert staged.read_bytes() == payload
        assert manager.used_bytes == 2 * len(payload)
        assert manager._committed_by_scope["owner-a"] == 2 * len(payload)

    retry = manager.reserve_storage("owner-a")
    assert manager.used_bytes == len(payload)
    assert not staged.exists()
    assert manager.get(existing.session_id) is existing
    retry.release()


@pytest.mark.anyio
async def test_internal_reservation_is_released_after_failed_managed_source_check(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    root.mkdir()
    manager = session.RenderSessionManager()
    staged = root / "pdfnest-session-upload-low-level.pdf"
    payload = b"synthetic staged input"
    staged.write_bytes(payload)

    with pytest.raises(session.SessionStorageLimitError, match="size changed"):
        await manager.create(
            staged,
            len(payload) + 1,
            hashlib.sha256(payload).hexdigest(),
            "owner-a",
        )

    assert not manager._sessions
    assert not staged.exists()
    assert manager.used_bytes == 0
    assert list(root.iterdir()) == []


@pytest.mark.anyio
async def test_rejected_unreserved_stage_remains_accounted_if_unlink_fails(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    monkeypatch.setattr(session, "MAX_SESSION_FILE_BYTES", 4)
    root.mkdir()
    manager = session.RenderSessionManager()
    staged = root / "pdfnest-session-upload-over-limit.pdf"
    payload = b"synthetic over-limit input"
    staged.write_bytes(payload)
    actual_unlink = Path.unlink

    def fail_staging_unlink(path, *args, **kwargs):
        if Path(path) == staged:
            raise OSError("synthetic filesystem denial")
        return actual_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_staging_unlink)
        with pytest.raises(session.SessionStorageLimitError, match="file exceeds"):
            await manager.create(
                staged,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                "owner-a",
            )
        assert staged.read_bytes() == payload
        assert manager.used_bytes == len(payload)
        assert len(manager._pending_deletions) == 1

    retry = manager.reserve_storage("owner-a")
    assert manager.used_bytes == 0
    assert not staged.exists()
    retry.release()


@pytest.mark.anyio
async def test_failed_upload_staging_remains_accounted_until_retry(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)
    payload = b"synthetic staged upload"

    class Upload:
        calls = 0

        async def read(self, _size):
            self.calls += 1
            return payload if self.calls == 1 else b""

    async def fail_creation(**_kwargs):
        raise RuntimeError("synthetic create failure")

    monkeypatch.setattr(manager, "get_or_create", fail_creation)
    actual_unlink = Path.unlink

    def fail_staging_unlink(path, *args, **kwargs):
        path = Path(path)
        if path.parent == root and path.name.startswith("pdfnest-session-upload-"):
            raise OSError("synthetic filesystem denial")
        return actual_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_staging_unlink)
        with pytest.raises(RuntimeError, match="synthetic create failure"):
            await service._create_render_session(Upload(), "owner-a")
        assert manager.used_bytes == len(payload)
        assert len(manager._pending_deletions) == 1
        staged_path = next(iter(manager._pending_deletions))
        assert staged_path.read_bytes() == payload

    retry = manager.reserve_storage("owner-a")
    assert manager.used_bytes == 0
    assert not staged_path.exists()
    retry.release()


@pytest.mark.anyio
async def test_cancelled_upload_releases_reservation_and_removes_partial_file(monkeypatch, tmp_path: Path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)

    class CancelAfterOneChunk:
        def __init__(self):
            self.calls = 0

        async def read(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"partial synthetic PDF"
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service._create_render_session(CancelAfterOneChunk(), "owner-a")

    assert manager.used_bytes == 0
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("name", ["WEB_CONCURRENCY", "UVICORN_WORKERS", "PDFNEST_RENDER_REPLICAS"])
@pytest.mark.parametrize("value", ["2", "0", "invalid"])
def test_render_topology_rejects_unsafe_or_unknown_counts(monkeypatch, name, value):
    for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "PDFNEST_RENDER_REPLICAS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        session.validate_render_topology()


def test_production_requires_explicit_single_replica_declaration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PDFNEST_RENDER_REPLICAS", raising=False)
    with pytest.raises(RuntimeError, match="PDFNEST_RENDER_REPLICAS"):
        session.validate_render_topology()
    monkeypatch.setenv("PDFNEST_RENDER_REPLICAS", "1")
    session.validate_render_topology()


def test_forked_manager_rejects_admission_and_access_without_touching_parent(monkeypatch, tmp_path):
    import os
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    existing = _register_synthetic_session(manager, root, "1" * 32, "owner", b"pdf")
    pid = os.fork()
    if pid == 0:
        try:
            for operation in (lambda: manager.reserve_storage("owner"),
                              lambda: manager.get(existing.session_id),
                              lambda: manager.delete(existing.session_id)):
                try:
                    operation()
                except RuntimeError:
                    continue
                os._exit(2)
            os._exit(0)
        except BaseException:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert manager.get(existing.session_id) is existing
    assert existing.file_path.read_bytes() == b"pdf"
    assert manager.used_bytes == 3


def test_process_lock_rejects_peer_and_restart_reconciles_killed_owner(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    root = tmp_path / "sessions"
    env = {**os.environ, "PDFNEST_RENDER_SESSION_DIR": str(root), "APP_ENV": "test"}
    owner_script = (
        "from app.api.tools.render.session import SESSION_ROOT; import time; "
        "p=SESSION_ROOT/('a'*32); p.mkdir(); (p/'source.pdf').write_bytes(b'orphan'); "
        "(SESSION_ROOT/'pdfnest-session-upload-crashed.pdf').write_bytes(b'partial'); "
        "d=SESSION_ROOT/'pdfnest-render-job-abcdefgh'; d.mkdir(); (d/'output.jpg').write_bytes(b'partial render'); "
        "print('ready',flush=True); time.sleep(60)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_script], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([owner.stdout], [], [], 10)[0], "owner did not become ready"
        assert owner.stdout.readline().strip() == "ready"
        peer = subprocess.run([sys.executable, "-c", "import app.api.tools.render.session"],
                              env=env, capture_output=True, text=True, timeout=10)
        assert peer.returncode != 0
        assert "already owned" in peer.stderr
        assert (root / ("a" * 32) / "source.pdf").exists()
        owner.send_signal(signal.SIGKILL)
        owner.wait(timeout=10)
        restarted = subprocess.run(
            [sys.executable, "-c", "from app.api.tools.render.session import SESSION_ROOT; "
             "assert sorted(p.name for p in SESSION_ROOT.iterdir()) == ['.manager.lock', '.render-root-protocol']"],
            env=env, capture_output=True, text=True, timeout=10)
        assert restarted.returncode == 0, restarted.stderr
    finally:
        owner.kill() if owner.poll() is None else None
        owner.communicate(timeout=10)


def test_production_rejects_unbounded_persistent_pool(monkeypatch):
    from dataclasses import replace
    from app.core import config
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PDFNEST_RENDER_REPLICAS", "1")
    monkeypatch.setattr(config, "settings", replace(config.settings, enable_persistent_render_pool=True))
    with pytest.raises(RuntimeError, match="persistent render pool"):
        session.validate_render_topology()


@pytest.mark.anyio
async def test_render_intermediates_reserve_shared_budget_before_child(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 10)
    monkeypatch.setattr(service, "MAX_RENDER_OUTPUT_BYTES", 8)
    existing = _register_synthetic_session(manager, root, "2" * 32, "owner-a", b"pdf")
    async def forbidden_child(*_args, **_kwargs):
        pytest.fail("native child started without storage capacity")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_child)
    with pytest.raises(session.SessionStorageLimitError):
        await service.render_page_to_jpeg_bytes_subprocess(b"%PDF", 1, 72)
    assert manager.used_bytes == 3
    assert manager.get(existing.session_id) is existing
    assert sorted(p.name for p in root.iterdir()) == ["2" * 32]


@pytest.mark.anyio
async def test_render_failed_output_cleanup_retains_liability(monkeypatch, tmp_path):
    import sys
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)
    monkeypatch.setattr(service, "MAX_RENDER_OUTPUT_BYTES", 8)
    def render(command):
        output = command[command.index("--output") + 1]
        return [sys.executable, "-c", "import sys;open(sys.argv[1],'wb').write(b'jpeg')", output]
    patch_render_child(monkeypatch, render)
    original_remove = session._remove_managed_path
    def fail_output_and_directory(path):
        if path.name == "output.jpg" or path.name.startswith("pdfnest-render-job-"):
            raise OSError("synthetic removal denial")
        return original_remove(path)
    with monkeypatch.context() as patch:
        patch.setattr(session, "_remove_managed_path", fail_output_and_directory)
        assert (await service.render_page_to_jpeg_bytes_subprocess(b"%PDF", 1, 72))[0] == b"jpeg"
        assert manager.used_bytes == 8, "full reserved output remains charged until removal is verified"
        assert len(list(root.glob("*/output.jpg"))) == 1
    retry = manager.reserve_storage("owner")
    retry.release()
    assert manager.used_bytes == 0
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("arguments", ["--workers 2", "--workers=2", "-w2", "-w 2"])
def test_render_topology_rejects_server_cli_multiprocess(monkeypatch, arguments):
    import shlex
    import sys
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.main:app", *shlex.split(arguments)])
    with pytest.raises(RuntimeError, match="worker arguments"):
        session.validate_render_topology()


def test_render_topology_rejects_gunicorn_multiprocess_environment(monkeypatch):
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 2")
    with pytest.raises(RuntimeError, match="worker arguments"):
        session.validate_render_topology()


@pytest.mark.anyio
async def test_concurrent_render_intermediates_cannot_overadmit_shared_budget(monkeypatch, tmp_path):
    import sys
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)
    monkeypatch.setattr(session, "MAX_SESSION_TOTAL_BYTES", 20)
    monkeypatch.setattr(service, "MAX_RENDER_OUTPUT_BYTES", 8)
    started, release = tmp_path / "started", tmp_path / "release"
    def render(command):
        output = command[command.index("--output") + 1]
        script = "import sys,time;from pathlib import Path;Path(sys.argv[2]).touch();\nwhile not Path(sys.argv[3]).exists():time.sleep(.01)\nPath(sys.argv[1]).write_bytes(b'jpeg')"
        return [sys.executable, "-c", script, output, str(started), str(release)]
    patch_render_child(monkeypatch, render)
    first = asyncio.create_task(service.render_page_to_jpeg_bytes_subprocess(b"%PDF", 1, 72))
    try:
        async with asyncio.timeout(5):
            while not started.exists(): await asyncio.sleep(.01)
        assert manager.used_bytes == 12
        with pytest.raises(session.SessionStorageLimitError):
            await service.render_page_to_jpeg_bytes_subprocess(b"%PDF", 1, 72)
        assert manager.used_bytes == 12
        assert len(list(root.iterdir())) == 1
        release.touch()
        assert (await first)[0] == b"jpeg"
    finally:
        release.touch()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert manager.used_bytes == 0
    assert list(root.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup", ["delete", "expiry", "eviction"])
async def test_open_render_source_keeps_deletion_liability_until_child_reaped(monkeypatch, tmp_path, cleanup):
    import sys
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, "session_manager", manager)
    monkeypatch.setattr(service, "MAX_RENDER_OUTPUT_BYTES", 8)
    existing = _register_synthetic_session(manager, root, "3" * 32, "owner-a", b"pdf")
    marker, release = tmp_path / "opened", tmp_path / "release"
    def render(command):
        source = command[command.index("--input") + 1]
        output = command[command.index("--output") + 1]
        script = (
            "import sys,time; from pathlib import Path; f=open(sys.argv[1],'rb'); "
            "Path(sys.argv[3]).touch(); "
            "\nwhile not Path(sys.argv[4]).exists(): time.sleep(.01)\n"
            "assert f.read()==b'pdf'; f.close(); Path(sys.argv[2]).write_bytes(b'jpeg')"
        )
        return [sys.executable, "-c", script, source, output, str(marker), str(release)]
    children = patch_render_child(monkeypatch, render)
    task = asyncio.create_task(service.get_render_session_page(existing.session_id, 1, 72))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        if cleanup == "delete":
            manager.delete(existing.session_id)
        elif cleanup == "expiry":
            existing.last_accessed_at = 0
            manager.get_optional(existing.session_id)
        else:
            with manager._lock:
                assert manager._evict_oldest_locked("owner-a")
        assert manager.get_optional(existing.session_id) is None
        assert existing.file_path.exists(), "open-source blocks must remain charged and managed"
        assert manager.used_bytes == existing.file_size + 8
        assert existing.active_renders == 1
        release.touch()
        assert await task == b"jpeg"
        assert children[0].returncode is not None
        assert existing.active_renders == 0
        assert manager.used_bytes == 0
        assert list(root.iterdir()) == []
    finally:
        release.touch()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("environment", [" production ", "staging", "canary", " production", "PRODUCTION "])
def test_managed_environment_cannot_bypass_topology_guards(monkeypatch, environment):
    from dataclasses import replace
    from app.core import config
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.delenv("PDFNEST_RENDER_REPLICAS", raising=False)
    with pytest.raises(RuntimeError, match="PDFNEST_RENDER_REPLICAS"):
        session.validate_render_topology()
    monkeypatch.setenv("PDFNEST_RENDER_REPLICAS", "1")
    monkeypatch.setattr(config, "settings", replace(config.settings, enable_persistent_render_pool=True))
    with pytest.raises(RuntimeError, match="persistent render pool"):
        session.validate_render_topology()


@pytest.mark.parametrize("artifact", ["4" * 32, "pdfnest-render-job-abcdefgh"])
def test_restart_refuses_unverified_writer_even_after_root_lock_is_released(tmp_path, artifact):
    root = tmp_path / "sessions"
    session._claim_session_root(root).close()
    directory = root / artifact
    directory.mkdir(parents=True)
    (directory / "writer.active").touch()
    (directory / "source.pdf").write_bytes(b"writer-owned bytes")
    with pytest.raises(RuntimeError, match="drainage is unverified"):
        session._claim_session_root(root)
    assert (directory / "source.pdf").read_bytes() == b"writer-owned bytes"
    # Only the actual guardian's post-reap receipt permits automatic recovery.
    (directory / "writer.drained").touch()
    lock = session._claim_session_root(root)
    lock.close()
    assert not directory.exists()


def test_missing_guardian_drain_receipt_quarantines_bytes_and_admission(tmp_path, monkeypatch):
    from app.api.tools.render.supervisor import PreviewProcessControl
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(session, "SESSION_ROOT", root)
    monkeypatch.setattr(session, "_SESSION_ROOT_QUARANTINED", False)
    manager = session.RenderSessionManager()
    existing = _register_synthetic_session(manager, root, "5" * 32, "owner", b"retained")
    control = PreviewProcessControl(session._SESSION_ROOT_LOCK.fileno())
    try:
        control.started = True
        with pytest.raises(RuntimeError, match="drainage is unverified"):
            control.verify()
        with pytest.raises(RuntimeError, match="quarantined"):
            manager.reserve_storage("owner")
        with pytest.raises(RuntimeError, match="quarantined"):
            manager.delete(existing.session_id)
        assert existing.file_path.read_bytes() == b"retained"
        assert manager._committed_bytes == existing.file_size
    finally:
        control.close()


def test_root_flock_survives_api_and_guardian_death_in_actual_writer(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    from app.controller.linux import pidfd_open, pidfd_signal
    root = tmp_path / "sessions"
    guardian_marker, writer_marker = tmp_path / "guardian.pid", tmp_path / "writer.pid"
    env = {**os.environ, "APP_ENV": "test", "PDFNEST_RENDER_SESSION_DIR": str(root)}
    writer = ("import os,time; from pathlib import Path; "
              f"Path({str(writer_marker)!r}).write_text(str(os.getpid())); time.sleep(60)")
    owner_script = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "from app.api.tools.render.session import _SESSION_ROOT_LOCK,SESSION_ROOT; "
        "from app.api.tools.render.supervisor import PreviewProcessControl; "
        "d=SESSION_ROOT/'pdfnest-render-job-abcdefgh'; d.mkdir(); "
        "c=PreviewProcessControl(_SESSION_ROOT_LOCK.fileno(),d/'writer.drained'); "
        f"p=subprocess.Popen([sys.executable,'-m','app.api.tools.render.supervisor',str(os.getpid()),sys.executable,'-c',{writer!r}],"
        "env=c.environment(os.environ),pass_fds=c.pass_fds); "
        f"Path({str(guardian_marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_script], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handles = []
    try:
        deadline = time.monotonic() + 10
        while not writer_marker.exists():
            assert owner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        guardian_pid = int(guardian_marker.read_text())
        writer_pid = int(writer_marker.read_text())
        guardian_fd, writer_fd = pidfd_open(guardian_pid), pidfd_open(writer_pid)
        handles += [guardian_fd, writer_fd]
        pidfd_signal(guardian_fd, signal.SIGSTOP)
        owner.kill()
        owner.wait(timeout=5)
        peer_command = [sys.executable, "-c", "import app.api.tools.render.session"]
        peer = subprocess.run(peer_command, env=env, capture_output=True, text=True, timeout=10)
        assert peer.returncode != 0 and "already owned" in peer.stderr
        pidfd_signal(guardian_fd, signal.SIGKILL)
        time.sleep(.05)
        peer = subprocess.run(peer_command, env=env, capture_output=True, text=True, timeout=10)
        assert peer.returncode != 0 and "already owned" in peer.stderr, "actual writer must retain the inherited root lock"
        pidfd_signal(writer_fd, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while True:
            peer = subprocess.run(peer_command, env=env, capture_output=True, text=True, timeout=10)
            if "already owned" not in peer.stderr:
                break
            assert time.monotonic() < deadline
        assert peer.returncode != 0 and "drainage is unverified" in peer.stderr
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        for descriptor in handles:
            try:
                pidfd_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(descriptor)


def test_root_protocol_is_initialized_on_empty_root_and_allows_marked_orphan_recovery(tmp_path):
    root = tmp_path / "sessions"
    claimed = session._claim_session_root(root)
    marker = root / session.ROOT_PROTOCOL_MARKER
    assert marker.read_bytes() == session.ROOT_PROTOCOL_VERSION
    marker_inode = marker.stat().st_ino
    claimed.close()
    orphan = root / "pdfnest-session-upload-marked-orphan.pdf"
    orphan.write_bytes(b"partial upload")
    restarted = session._claim_session_root(root)
    restarted.close()
    assert not orphan.exists()
    assert marker.read_bytes() == session.ROOT_PROTOCOL_VERSION
    assert marker.stat().st_ino == marker_inode


@pytest.mark.parametrize("artifact", [".manager.lock", "pdfnest-session-upload-legacy.pdf", "6" * 32])
def test_nonempty_unversioned_root_is_rejected_without_modification(tmp_path, artifact):
    root = tmp_path / "legacy"
    root.mkdir(mode=0o755)
    path = root / artifact
    if len(artifact) == 32:
        path.mkdir()
        (path / "source.pdf").write_bytes(b"legacy document")
    else:
        path.write_bytes(b"legacy bytes")
    before = {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
              for p in [root, *root.rglob("*")]}
    with pytest.raises(RuntimeError, match="nonempty unversioned"):
        session._claim_session_root(root)
    after = {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
             for p in [root, *root.rglob("*")]}
    assert after == before
    assert not (root / session.ROOT_PROTOCOL_MARKER).exists()


def test_legacy_root_with_unlocked_old_native_writer_is_rejected_unchanged(tmp_path):
    import os
    import subprocess
    import sys
    root = tmp_path / "legacy"
    root.mkdir(mode=0o755)
    directory = root / ("7" * 32)
    directory.mkdir()
    source = directory / "source.pdf"
    source.write_bytes(b"legacy source")
    (root / ".manager.lock").write_bytes(b"old lock inode")
    opened, release = tmp_path / "opened", tmp_path / "release"
    script = (
        "import sys,time;from pathlib import Path;f=open(sys.argv[1],'r+b');"
        "Path(sys.argv[2]).touch();"
        "\nwhile not Path(sys.argv[3]).exists():time.sleep(.01)\n"
        "f.seek(0,2);f.write(b'+writer');f.flush();f.close()"
    )
    writer = subprocess.Popen([sys.executable, "-c", script, str(source), str(opened), str(release)])
    try:
        deadline = time.monotonic() + 5
        while not opened.exists():
            assert writer.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        before_mode, before_inode = root.stat().st_mode, source.stat().st_ino
        env = {**os.environ, "PDFNEST_RENDER_SESSION_DIR": str(root), "APP_ENV": "test"}
        newcomer = subprocess.run([sys.executable, "-c", "import app.api.tools.render.session"],
                                 env=env, capture_output=True, text=True, timeout=10)
        assert newcomer.returncode != 0 and "nonempty unversioned" in newcomer.stderr
        assert root.stat().st_mode == before_mode
        assert source.stat().st_ino == before_inode
        assert source.read_bytes() == b"legacy source"
        assert (root / ".manager.lock").read_bytes() == b"old lock inode"
        assert not (root / session.ROOT_PROTOCOL_MARKER).exists()
        release.touch()
        writer.wait(timeout=5)
        assert writer.returncode == 0
        assert source.read_bytes() == b"legacy source+writer", "the old writer must retain its linked source"
    finally:
        release.touch()
        if writer.poll() is None:
            writer.kill()
        writer.wait(timeout=5)

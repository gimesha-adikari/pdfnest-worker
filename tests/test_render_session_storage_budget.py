from __future__ import annotations

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
    stale_session = root / "0123456789abcdef0123456789abcdef"
    stale_session.mkdir(parents=True)
    (stale_session / "source.pdf").write_bytes(b"synthetic stale data")
    stale_upload = root / "pdfnest-session-upload-orphan.pdf"
    stale_upload.write_bytes(b"synthetic partial upload")

    lock = session._claim_session_root(root)
    try:
        assert set(root.iterdir()) == {root / ".manager.lock"}
        with pytest.raises(RuntimeError, match="already owned"):
            session._claim_session_root(root)
    finally:
        lock.close()


def test_session_root_restart_fails_closed_when_orphan_cleanup_fails(tmp_path: Path, monkeypatch):
    root = tmp_path / "sessions"
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
        assert set(root.iterdir()) == {root / ".manager.lock"}
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

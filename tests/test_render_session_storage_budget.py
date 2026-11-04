from __future__ import annotations

import asyncio
import hashlib
import threading
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

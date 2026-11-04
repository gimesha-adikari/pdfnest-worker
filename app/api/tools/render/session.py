from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .renderer import PdfRenderDocument
from .session_metadata import inspect_page_count, session_file_io


SESSION_ROOT = Path(
    os.getenv(
        "PDFNEST_RENDER_SESSION_DIR",
        "/tmp/pdfnest-render-sessions",
    )
)

def _bounded_positive_env(name: str, default: int, hard_max: int) -> int:
    raw = os.getenv(name)
    try:
        configured = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if configured <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return min(configured, hard_max)


SESSION_TTL_SECONDS = _bounded_positive_env("PDFNEST_RENDER_SESSION_TTL", 1800, 1800)
MAX_SESSIONS = _bounded_positive_env("PDFNEST_RENDER_MAX_SESSIONS", 32, 32)
MAX_SESSION_FILE_BYTES = _bounded_positive_env("PDFNEST_RENDER_MAX_SESSION_BYTES", 64 * 1024 * 1024, 64 * 1024 * 1024)
MAX_SESSION_OWNER_BYTES = _bounded_positive_env("PDFNEST_RENDER_MAX_OWNER_BYTES", 128 * 1024 * 1024, 128 * 1024 * 1024)
MAX_SESSION_TOTAL_BYTES = _bounded_positive_env("PDFNEST_RENDER_MAX_TOTAL_BYTES", 256 * 1024 * 1024, 256 * 1024 * 1024)


def _claim_session_root(root: Path):
    """Own one session root and remove only known stale session artifacts.

    Storage accounting is process-local. A nonblocking process lock makes that
    limit meaningful by refusing a second manager process on the same local
    session filesystem. The lock is released by the OS after a crash; the next
    process can then safely remove orphaned session directories and upload
    staging files before accepting work.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("render session root must be a real directory")
    root.chmod(0o700)

    lock_path = root / ".manager.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("unable to open render session manager lock") from exc
    lock_file = os.fdopen(lock_fd, "r+b", closefd=True)
    try:
        os.fchmod(lock_file.fileno(), 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("render session root is already owned by another process") from exc

        for entry in root.iterdir():
            if entry.name == ".manager.lock":
                continue
            if re.fullmatch(r"[0-9a-f]{32}", entry.name):
                if not stat.S_ISDIR(entry.lstat().st_mode):
                    raise RuntimeError("unexpected render session artifact type")
                shutil.rmtree(entry)
                continue
            if re.fullmatch(r"pdfnest-session-upload-[^/]+\.pdf", entry.name):
                mode = entry.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise RuntimeError("unexpected render upload staging artifact type")
                entry.unlink()
                continue
            raise RuntimeError("unexpected file in dedicated render session root")
        return lock_file
    except BaseException:
        lock_file.close()
        raise


_SESSION_ROOT_LOCK = _claim_session_root(SESSION_ROOT)


@dataclass
class RenderSession:
    session_id: str
    file_path: Path
    file_size: int
    sha256: str
    page_count: int
    created_at: float
    last_accessed_at: float
    dedup_scope: str = "global"
    document: PdfRenderDocument | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class _InFlightCreation:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    session: RenderSession | None = None
    error: BaseException | None = None
    waiters: int = 0


class SessionStorageLimitError(RuntimeError):
    """A render upload would exceed a hard in-process storage budget."""


class SessionStorageReservation:
    def __init__(self, manager: "RenderSessionManager", scope: str) -> None:
        self.manager = manager
        self.scope = scope
        self.bytes_reserved = 0
        self.active = True

    def extend(self, amount: int) -> None:
        self.manager._extend_reservation(self, amount)

    def release(self) -> None:
        self.manager._release_reservation(self)


class RenderSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, RenderSession] = {}
        self._lock = threading.RLock()
        self._inflight: dict[tuple[str, str], _InFlightCreation] = {}
        self._committed_bytes = 0
        self._committed_by_scope: dict[str, int] = {}
        self._reserved_bytes = 0
        self._reserved_by_scope: dict[str, int] = {}

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._committed_bytes + self._reserved_bytes

    def reserve_storage(self, scope: str) -> SessionStorageReservation:
        return SessionStorageReservation(self, scope)

    def _extend_reservation(self, reservation: SessionStorageReservation, amount: int) -> None:
        if type(amount) is not int or amount < 0:
            raise SessionStorageLimitError("invalid render-session storage reservation")
        if amount == 0:
            return
        with self._lock:
            if not reservation.active:
                raise SessionStorageLimitError("render-session storage reservation is no longer active")
            next_file = reservation.bytes_reserved + amount
            next_total = self._committed_bytes + self._reserved_bytes + amount
            next_owner = (
                self._committed_by_scope.get(reservation.scope, 0)
                + self._reserved_by_scope.get(reservation.scope, 0)
                + amount
            )
            if next_file > MAX_SESSION_FILE_BYTES:
                raise SessionStorageLimitError("render-session file exceeds its byte limit")
            if next_total > MAX_SESSION_TOTAL_BYTES:
                raise SessionStorageLimitError("render-session aggregate storage budget is full")
            if reservation.scope != "global" and next_owner > MAX_SESSION_OWNER_BYTES:
                raise SessionStorageLimitError("render-session owner storage budget is full")
            reservation.bytes_reserved = next_file
            self._reserved_bytes += amount
            self._reserved_by_scope[reservation.scope] = self._reserved_by_scope.get(reservation.scope, 0) + amount

    def _release_reservation(self, reservation: SessionStorageReservation) -> None:
        with self._lock:
            if not reservation.active:
                return
            self._reserved_bytes -= reservation.bytes_reserved
            scope_bytes = self._reserved_by_scope.get(reservation.scope, 0) - reservation.bytes_reserved
            if scope_bytes > 0:
                self._reserved_by_scope[reservation.scope] = scope_bytes
            else:
                self._reserved_by_scope.pop(reservation.scope, None)
            reservation.bytes_reserved = 0
            reservation.active = False

    def _commit_reservation(self, reservation: SessionStorageReservation, session: RenderSession) -> None:
        if not reservation.active or reservation.bytes_reserved != session.file_size:
            raise SessionStorageLimitError("render-session storage reservation does not match the file")
        self._reserved_bytes -= reservation.bytes_reserved
        reserved_scope = self._reserved_by_scope.get(reservation.scope, 0) - reservation.bytes_reserved
        if reserved_scope > 0:
            self._reserved_by_scope[reservation.scope] = reserved_scope
        else:
            self._reserved_by_scope.pop(reservation.scope, None)
        self._committed_bytes += session.file_size
        self._committed_by_scope[session.dedup_scope] = self._committed_by_scope.get(session.dedup_scope, 0) + session.file_size
        reservation.bytes_reserved = 0
        reservation.active = False

    def _release_session_storage_locked(self, session: RenderSession) -> None:
        self._committed_bytes = max(0, self._committed_bytes - session.file_size)
        scope_bytes = self._committed_by_scope.get(session.dedup_scope, 0) - session.file_size
        if scope_bytes > 0:
            self._committed_by_scope[session.dedup_scope] = scope_bytes
        else:
            self._committed_by_scope.pop(session.dedup_scope, None)

    async def create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
            reservation: SessionStorageReservation | None = None,
    ) -> RenderSession:
        if reservation is None:
            reservation = self.reserve_storage(dedup_scope)
            try:
                reservation.extend(file_size)
            except BaseException:
                reservation.release()
                raise
        elif reservation.scope != dedup_scope:
            raise SessionStorageLimitError("render-session reservation scope does not match")
        elif reservation.bytes_reserved < file_size:
            reservation.extend(file_size - reservation.bytes_reserved)

        session_id = uuid.uuid4().hex
        session_dir = SESSION_ROOT / session_id
        target_path = session_dir / "source.pdf"
        published = False
        try:
            session_dir.mkdir(mode=0o700, parents=True)
            # Uploads are staged under SESSION_ROOT and atomically moved so
            # accounting never ignores a second full-sized copy.
            if file_path.stat().st_size != file_size:
                raise SessionStorageLimitError("render-session file size changed during admission")
            source_inside_session_root = file_path.resolve().is_relative_to(SESSION_ROOT.resolve())
            if source_inside_session_root:
                if file_path.stat().st_dev != SESSION_ROOT.stat().st_dev:
                    raise SessionStorageLimitError("render-session staging must use the bounded session filesystem")
                await session_file_io(os.replace, file_path, target_path)
            else:
                # Keep the manager's long-standing non-destructive contract for
                # callers that stage elsewhere. The upload endpoint stages
                # inside SESSION_ROOT and therefore uses the accounted move.
                await session_file_io(shutil.copyfile, file_path, target_path)
            target_path.chmod(0o600)
            page_count = await inspect_page_count(target_path)
            with self._lock:
                self._cleanup_expired_locked()
                # Recheck after native work: simultaneous submissions may
                # have published or deleted a matching session in the meantime.
                for existing in self._sessions.values():
                    if existing.sha256 == sha256 and existing.dedup_scope == dedup_scope:
                        existing.last_accessed_at = time.time()
                        return existing
                if len(self._sessions) >= MAX_SESSIONS:
                    self._evict_oldest_locked()
                now = time.time()
                session = RenderSession(
                    session_id=session_id,
                    file_path=target_path,
                    file_size=file_size,
                    sha256=sha256,
                    page_count=page_count,
                    created_at=now,
                    last_accessed_at=now,
                    dedup_scope=dedup_scope,
                    document=None,
                )
                # No await between publication, accounting and return.
                self._sessions[session_id] = session
                self._commit_reservation(reservation, session)
                published = True
                return session
        finally:
            if not published:
                shutil.rmtree(session_dir, ignore_errors=True)
            reservation.release()

    async def get_or_create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
            reservation: SessionStorageReservation | None = None,
    ) -> RenderSession:
        key = (sha256, dedup_scope)
        max_waiters = 16
        wait_timeout = 25.0

        while True:
            is_leader = False
            inflight: _InFlightCreation | None = None

            with self._lock:
                self._cleanup_expired_locked()
                for existing in self._sessions.values():
                    if existing.sha256 == sha256 and existing.dedup_scope == dedup_scope:
                        existing.last_accessed_at = time.time()
                        if reservation is not None:
                            reservation.release()
                        return existing

                inflight = self._inflight.get(key)
                if inflight is None:
                    inflight = _InFlightCreation()
                    self._inflight[key] = inflight
                    is_leader = True
                else:
                    if inflight.waiters >= max_waiters:
                        from .session_metadata import MetadataBusy
                        raise MetadataBusy()
                    inflight.waiters += 1

            if is_leader:
                created_session: RenderSession | None = None
                creation_error: BaseException | None = None
                try:
                    created_session = await self.create(
                        file_path=file_path,
                        file_size=file_size,
                        sha256=sha256,
                        dedup_scope=dedup_scope,
                        reservation=reservation,
                    )
                    return created_session
                except BaseException as exc:
                    creation_error = exc
                    raise
                finally:
                    with self._lock:
                        inflight.session = created_session
                        inflight.error = creation_error
                        self._inflight.pop(key, None)
                        inflight.event.set()
            else:
                # A matching leader owns verification. Only delete request
                # staging files that this manager owns; callers may provide a
                # shared external path to the lower-level manager API.
                try:
                    resolved_root = SESSION_ROOT.resolve()
                    resolved_file = file_path.resolve()
                    if resolved_file != resolved_root and resolved_root in resolved_file.parents:
                        file_path.unlink(missing_ok=True)
                finally:
                    if reservation is not None:
                        reservation.release()
                try:
                    await asyncio.wait_for(inflight.event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError as exc:
                    from .session_metadata import MetadataTimeout
                    raise MetadataTimeout() from exc
                finally:
                    with self._lock:
                        inflight.waiters -= 1

                if inflight.session is not None:
                    return inflight.session

                if inflight.error is not None and not isinstance(inflight.error, asyncio.CancelledError):
                    raise inflight.error

    def get(self, session_id: str) -> RenderSession:
        with self._lock:
            self._cleanup_expired_locked()

            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Render session '{session_id}' was not found")

            session.last_accessed_at = time.time()
            return session

    def get_optional(self, session_id: str) -> RenderSession | None:
        with self._lock:
            self._cleanup_expired_locked()

            session = self._sessions.get(session_id)
            if session is None:
                return None

            session.last_accessed_at = time.time()
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                self._release_session_storage_locked(session)

        if session is None:
            return

        with session.lock:
            if session.document is not None:
                session.document.close()
                session.document = None

        try:
            shutil.rmtree(session.file_path.parent, ignore_errors=True)
        except Exception:
            pass

    def find_by_hash(self, sha256: str, dedup_scope: str = "global") -> RenderSession | None:
        with self._lock:
            self._cleanup_expired_locked()

            for session in self._sessions.values():
                if session.sha256 == sha256 and session.dedup_scope == dedup_scope:
                    session.last_accessed_at = time.time()
                    return session

            return None

    def _evict_oldest_locked(self) -> None:
        if not self._sessions:
            return

        oldest = min(
            self._sessions.values(),
            key=lambda item: item.last_accessed_at,
        )

        self._sessions.pop(oldest.session_id, None)
        self._release_session_storage_locked(oldest)

        try:
            if oldest.document is not None:
                oldest.document.close()
                oldest.document = None
        except Exception:
            pass

        try:
            shutil.rmtree(
                oldest.file_path.parent,
                ignore_errors=True,
            )
        except Exception:
            pass

    def _cleanup_expired_locked(self) -> None:
        now = time.time()

        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_accessed_at > SESSION_TTL_SECONDS
        ]

        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session is None:
                continue
            self._release_session_storage_locked(session)

            try:
                if session.document is not None:
                    session.document.close()
                    session.document = None
            except Exception:
                pass

            try:
                shutil.rmtree(
                    session.file_path.parent,
                    ignore_errors=True,
                )
            except Exception:
                pass


session_manager = RenderSessionManager()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()

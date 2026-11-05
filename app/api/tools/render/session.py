from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
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


def _remove_managed_path(path: Path) -> None:
    """Remove one owned path without following symlinks, and verify it is gone."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return

    try:
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            # This also removes a symlink itself; it never follows the target.
            path.unlink()
    except FileNotFoundError:
        pass

    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise OSError(f"render storage path remains after cleanup: {path.name}")


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
                if (entry / "writer.active").exists() and not (entry / "writer.drained").exists():
                    raise RuntimeError("render writer drainage is unverified at restart")
                _remove_managed_path(entry)
                continue
            if re.fullmatch(r"pdfnest-render-job-[a-z0-9_]{8}", entry.name):
                if not stat.S_ISDIR(entry.lstat().st_mode):
                    raise RuntimeError("unexpected render intermediate artifact type")
                if (entry / "writer.active").exists() and not (entry / "writer.drained").exists():
                    raise RuntimeError("render writer drainage is unverified at restart")
                _remove_managed_path(entry)
                continue
            if re.fullmatch(r"pdfnest-session-upload-[^/]+\.pdf", entry.name):
                mode = entry.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise RuntimeError("unexpected render upload staging artifact type")
                _remove_managed_path(entry)
                continue
            raise RuntimeError("unexpected file in dedicated render session root")
        return lock_file
    except BaseException:
        lock_file.close()
        raise


_SESSION_ROOT_LOCK = _claim_session_root(SESSION_ROOT)
_SESSION_ROOT_OWNER_PID = os.getpid()
_SESSION_ROOT_QUARANTINED = False


def quarantine_render_root() -> None:
    global _SESSION_ROOT_QUARANTINED
    _SESSION_ROOT_QUARANTINED = True


def assert_render_process_owner() -> None:
    # A preloaded server can inherit both the flock's open description and the
    # in-memory accounting. Never let a fork use either before taking a lock.
    if os.getpid() != _SESSION_ROOT_OWNER_PID:
        raise RuntimeError("render manager cannot be used by a forked API process")
    if _SESSION_ROOT_QUARANTINED:
        raise RuntimeError("render storage is quarantined after unverified writer drainage")


def validate_render_topology() -> None:
    """Fail closed for declared scaling; local budgets have no shared backend.

    The replica declaration is an operator contract, not discovery of remote
    instances. Independent filesystems cannot be coordinated by the root lock.
    """
    from app.core.config import is_managed_environment, settings
    assert_render_process_owner()
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "PDFNEST_RENDER_REPLICAS"):
        value = os.getenv(name)
        if value is None:
            continue
        try:
            count = int(value)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be 1: render budgets require a single API process and replica") from exc
        if count != 1:
            raise RuntimeError(f"{name} must be 1: shared render admission is not implemented")
    try:
        server_args = shlex.split(os.getenv("GUNICORN_CMD_ARGS", ""))
    except ValueError as exc:
        raise RuntimeError("GUNICORN_CMD_ARGS must declare a single API worker") from exc
    if Path(sys.argv[0]).name in ("uvicorn", "gunicorn"):
        server_args += sys.argv[1:]
    for index, argument in enumerate(server_args):
        if argument in ("--workers", "-w"):
            value = server_args[index + 1] if index + 1 < len(server_args) else ""
        elif argument.startswith("--workers="):
            value = argument.split("=", 1)[1]
        elif argument.startswith("-w") and len(argument) > 2:
            value = argument[2:]
        else:
            continue
        if value != "1":
            raise RuntimeError("server worker arguments must select 1 process; shared render admission is not implemented")
    if is_managed_environment() and os.getenv("PDFNEST_RENDER_REPLICAS") is None:
        raise RuntimeError("PDFNEST_RENDER_REPLICAS=1 is required in managed environments; shared render admission is not implemented")
    if is_managed_environment():
        if settings.enable_persistent_render_pool:
            raise RuntimeError("persistent render pool lacks the required disposable-child resource limits")


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
    active_renders: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class _InFlightCreation:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    session: RenderSession | None = None
    error: BaseException | None = None
    waiters: int = 0


@dataclass
class _PendingStorageDeletion:
    path: Path
    bytes_reserved: int
    scope: str
    session: RenderSession | None = None


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
        # Pending paths remain included in committed accounting until their
        # physical removal is verified. Paths are exact manager-owned session
        # directories or upload staging files; cleanup never sweeps by owner.
        self._pending_deletions: dict[Path, _PendingStorageDeletion] = {}

    @property
    def used_bytes(self) -> int:
        assert_render_process_owner()
        with self._lock:
            return self._committed_bytes + self._reserved_bytes

    def reserve_storage(self, scope: str) -> SessionStorageReservation:
        assert_render_process_owner()
        with self._lock:
            self._retry_pending_deletions_locked()
            return SessionStorageReservation(self, scope)

    def _extend_reservation(self, reservation: SessionStorageReservation, amount: int) -> None:
        assert_render_process_owner()
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
        assert_render_process_owner()
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

    def _release_committed_storage_locked(self, scope: str, amount: int) -> None:
        self._committed_bytes -= amount
        scope_bytes = self._committed_by_scope.get(scope, 0) - amount
        if scope_bytes > 0:
            self._committed_by_scope[scope] = scope_bytes
        else:
            self._committed_by_scope.pop(scope, None)

    def _commit_reservation_as_pending_locked(
            self,
            reservation: SessionStorageReservation,
            path: Path,
            minimum_bytes: int = 0,
    ) -> None:
        if not reservation.active:
            return
        reserved_bytes = reservation.bytes_reserved
        amount = max(reserved_bytes, minimum_bytes)
        self._reserved_bytes -= reserved_bytes
        reserved_scope = self._reserved_by_scope.get(reservation.scope, 0) - reserved_bytes
        if reserved_scope > 0:
            self._reserved_by_scope[reservation.scope] = reserved_scope
        else:
            self._reserved_by_scope.pop(reservation.scope, None)
        self._committed_bytes += amount
        self._committed_by_scope[reservation.scope] = (
            self._committed_by_scope.get(reservation.scope, 0) + amount
        )
        self._pending_deletions[path] = _PendingStorageDeletion(
            path=path,
            bytes_reserved=amount,
            scope=reservation.scope,
        )
        reservation.bytes_reserved = 0
        reservation.active = False

    def _record_unreserved_pending_locked(self, path: Path, amount: int, scope: str) -> None:
        if path in self._pending_deletions:
            return
        self._committed_bytes += amount
        self._committed_by_scope[scope] = self._committed_by_scope.get(scope, 0) + amount
        self._pending_deletions[path] = _PendingStorageDeletion(
            path=path,
            bytes_reserved=amount,
            scope=scope,
        )

    def _cleanup_unpublished_path(
            self,
            path: Path,
            reservation: SessionStorageReservation,
            *,
            release_on_success: bool = True,
            retain_reservation_on_failure: bool = False,
            minimum_bytes_on_failure: int = 0,
    ) -> None:
        with self._lock:
            if _SESSION_ROOT_QUARANTINED:
                if retain_reservation_on_failure:
                    self._record_unreserved_pending_locked(path, minimum_bytes_on_failure, reservation.scope)
                else:
                    self._commit_reservation_as_pending_locked(reservation, path, minimum_bytes_on_failure)
                return
            if path in self._pending_deletions:
                self._retry_pending_deletions_locked({path})
                reservation.release()
                return
        try:
            _remove_managed_path(path)
        except OSError:
            # The reservation remains live during the attempt, then becomes a
            # committed liability if the bytes could not be physically removed.
            with self._lock:
                if retain_reservation_on_failure:
                    # Source bytes still belong to their upload staging path.
                    # Track this exact candidate directory separately so an
                    # empty or partially-created orphan is retried as well.
                    candidate_bytes = 0
                    candidate_file = path / "source.pdf"
                    try:
                        info = candidate_file.lstat()
                        if stat.S_ISREG(info.st_mode):
                            candidate_bytes = info.st_size
                    except FileNotFoundError:
                        pass
                    except OSError:
                        candidate_bytes = minimum_bytes_on_failure
                    self._record_unreserved_pending_locked(
                        path,
                        candidate_bytes,
                        reservation.scope,
                    )
                else:
                    self._commit_reservation_as_pending_locked(
                        reservation,
                        path,
                        minimum_bytes=minimum_bytes_on_failure,
                    )
        else:
            if release_on_success:
                reservation.release()

    def _cleanup_unreserved_upload(self, path: Path, scope: str, size_hint: int) -> None:
        # Low-level callers may supply a staging path without a reservation.
        # Serialize the removal attempt with admissions; if it fails, convert
        # the extant file into committed liability before releasing the lock.
        with self._lock:
            if path in self._pending_deletions:
                self._retry_pending_deletions_locked({path})
                return
            try:
                _remove_managed_path(path)
            except OSError:
                try:
                    amount = path.lstat().st_size
                except OSError:
                    amount = size_hint
                self._record_unreserved_pending_locked(path, amount, scope)

    def cleanup_upload_staging(
            self,
            path: Path | None,
            reservation: SessionStorageReservation | None,
            scope: str = "global",
            size_hint: int = 0,
    ) -> None:
        """Remove this request's upload file or retain its reserved liability."""
        assert_render_process_owner()
        if path is None:
            if reservation is not None:
                reservation.release()
            return
        # Upload paths are created directly under the claimed root. Use the
        # lexical parent and generated-name contract so a resolution error can
        # never turn an owned file into an unaccounted external path.
        owned = (
            path.parent == SESSION_ROOT
            and re.fullmatch(r"pdfnest-session-upload-[^/]+\.pdf", path.name) is not None
        )
        if owned:
            if reservation is None:
                self._cleanup_unreserved_upload(path, scope, size_hint)
            else:
                try:
                    path_info = path.lstat()
                    if stat.S_ISREG(path_info.st_mode):
                        size_hint = max(size_hint, path_info.st_size)
                except OSError:
                    pass
                self._cleanup_unpublished_path(
                    path,
                    reservation,
                    minimum_bytes_on_failure=size_hint,
                )
        else:
            # Lower-level callers may pass external paths, which remain owned by
            # their caller and must not be deleted here.
            if reservation is not None:
                reservation.release()

    def cleanup_render_staging(self, path: Path, reservation: SessionStorageReservation) -> None:
        """Retain byte liability when removal of a bounded render file fails."""
        assert_render_process_owner()
        directory = path.parent
        if directory.parent != SESSION_ROOT or re.fullmatch(r"pdfnest-render-job-[a-z0-9_]{8}", directory.name) is None:
            raise RuntimeError("render staging path is outside the managed root")
        self._cleanup_unpublished_path(path, reservation)

    def cleanup_render_directory(self, path: Path) -> None:
        assert_render_process_owner()
        if path.parent != SESSION_ROOT or re.fullmatch(r"pdfnest-render-job-[a-z0-9_]{8}", path.name) is None:
            raise RuntimeError("render staging directory is outside the managed root")
        reservation = self.reserve_storage("global")
        self._cleanup_unpublished_path(path, reservation)

    def _schedule_session_deletion_locked(self, session: RenderSession) -> Path:
        path = session.file_path.parent
        self._sessions.pop(session.session_id, None)
        self._pending_deletions[path] = _PendingStorageDeletion(
            path=path,
            bytes_reserved=session.file_size,
            scope=session.dedup_scope,
            session=session,
        )
        return path

    def _retry_pending_deletions_locked(self, paths: set[Path] | None = None) -> None:
        if _SESSION_ROOT_QUARANTINED:
            return
        for path, pending in tuple(self._pending_deletions.items()):
            if paths is not None and path not in paths:
                continue
            if pending.session is not None:
                # Unlinking an open source would release accounting while a
                # render child still pins its disk blocks through a descriptor.
                if pending.session.active_renders:
                    continue
                with pending.session.lock:
                    document = pending.session.document
                    if document is not None:
                        try:
                            document.close()
                        except Exception:
                            # Keep the path and accounting until close and
                            # physical cleanup can both be retried.
                            continue
                        pending.session.document = None
            try:
                _remove_managed_path(path)
            except OSError:
                continue
            current = self._pending_deletions.pop(path, None)
            if current is pending:
                self._release_committed_storage_locked(pending.scope, pending.bytes_reserved)

    async def create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
            reservation: SessionStorageReservation | None = None,
    ) -> RenderSession:
        assert_render_process_owner()
        supplied_reservation = reservation is not None
        if reservation is None:
            reservation = self.reserve_storage(dedup_scope)
            try:
                reservation.extend(file_size)
            except BaseException:
                self.cleanup_upload_staging(
                    file_path,
                    reservation,
                    scope=dedup_scope,
                    size_hint=file_size,
                )
                raise
        elif reservation.scope != dedup_scope:
            raise SessionStorageLimitError("render-session reservation scope does not match")
        elif reservation.bytes_reserved < file_size:
            reservation.extend(file_size - reservation.bytes_reserved)
        elif reservation.bytes_reserved > file_size:
            raise SessionStorageLimitError("render-session reservation exceeds the file size")

        session_id = uuid.uuid4().hex
        session_dir = SESSION_ROOT / session_id
        target_path = session_dir / "source.pdf"
        published = False
        owns_session_dir = False
        source_inside_session_root = False
        try:
            source_inside_session_root = file_path.parent == SESSION_ROOT
            if not source_inside_session_root:
                source_inside_session_root = file_path.resolve().is_relative_to(SESSION_ROOT.resolve())
            session_dir.mkdir(mode=0o700, parents=True)
            owns_session_dir = True
            # Uploads are staged under SESSION_ROOT and atomically moved so
            # accounting never ignores a second full-sized copy.
            if file_path.stat().st_size != file_size:
                raise SessionStorageLimitError("render-session file size changed during admission")
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
                    if not self._evict_oldest_locked(dedup_scope):
                        raise SessionStorageLimitError(
                            "render-session count limit is full for this owner"
                        )
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
                try:
                    self._commit_reservation(reservation, session)
                except BaseException:
                    self._sessions.pop(session_id, None)
                    raise
                published = True
                return session
        finally:
            if not published:
                if owns_session_dir:
                    source_still_staged = source_inside_session_root and os.path.lexists(file_path)
                    self._cleanup_unpublished_path(
                        session_dir,
                        reservation,
                        release_on_success=not source_still_staged,
                        retain_reservation_on_failure=source_still_staged,
                    )
                if not supplied_reservation and source_inside_session_root:
                    self.cleanup_upload_staging(
                        file_path,
                        reservation,
                        scope=dedup_scope,
                        size_hint=file_size,
                    )
                elif not supplied_reservation or not source_inside_session_root:
                    reservation.release()
            else:
                reservation.release()

    async def get_or_create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
            reservation: SessionStorageReservation | None = None,
    ) -> RenderSession:
        assert_render_process_owner()
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
                        self.cleanup_upload_staging(
                            file_path,
                            reservation,
                            scope=dedup_scope,
                            size_hint=file_size,
                        )
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
                self.cleanup_upload_staging(
                    file_path,
                    reservation,
                    scope=dedup_scope,
                    size_hint=file_size,
                )
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
        assert_render_process_owner()
        with self._lock:
            self._cleanup_expired_locked()

            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Render session '{session_id}' was not found")

            session.last_accessed_at = time.time()
            return session

    @contextmanager
    def render_source(self, session_id: str):
        assert_render_process_owner()
        with self._lock:
            session = self.get(session_id)
            session.active_renders += 1
        try:
            yield session
        finally:
            with self._lock:
                session.active_renders -= 1
                self._retry_pending_deletions_locked({session.file_path.parent})

    def get_optional(self, session_id: str) -> RenderSession | None:
        assert_render_process_owner()
        with self._lock:
            self._cleanup_expired_locked()

            session = self._sessions.get(session_id)
            if session is None:
                return None

            session.last_accessed_at = time.time()
            return session

    def delete(self, session_id: str) -> None:
        assert_render_process_owner()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                path = self._schedule_session_deletion_locked(session)
                self._retry_pending_deletions_locked({path})
                return

            pending_path = next((
                path
                for path, pending in self._pending_deletions.items()
                if pending.session is not None and pending.session.session_id == session_id
            ), None)
            if pending_path is not None:
                self._retry_pending_deletions_locked({pending_path})

    def find_by_hash(self, sha256: str, dedup_scope: str = "global") -> RenderSession | None:
        assert_render_process_owner()
        with self._lock:
            self._cleanup_expired_locked()

            for session in self._sessions.values():
                if session.sha256 == sha256 and session.dedup_scope == dedup_scope:
                    session.last_accessed_at = time.time()
                    return session

            return None

    def _evict_oldest_locked(self, dedup_scope: str | None = None) -> bool:
        candidates = [
            session
            for session in self._sessions.values()
            if dedup_scope is None or session.dedup_scope == dedup_scope
        ]
        if not candidates:
            return False

        oldest = min(candidates, key=lambda item: item.last_accessed_at)

        path = self._schedule_session_deletion_locked(oldest)
        self._retry_pending_deletions_locked({path})
        return True

    def _cleanup_expired_locked(self) -> None:
        now = time.time()

        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_accessed_at > SESSION_TTL_SECONDS
        ]

        for session_id in expired:
            session = self._sessions.get(session_id)
            if session is None:
                continue
            self._schedule_session_deletion_locked(session)
        self._retry_pending_deletions_locked()


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

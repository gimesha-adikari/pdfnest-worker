from __future__ import annotations

import hashlib
import os
import shutil
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

SESSION_TTL_SECONDS = int(
    os.getenv(
        "PDFNEST_RENDER_SESSION_TTL",
        "1800",
    )
)

MAX_SESSIONS = int(
    os.getenv(
        "PDFNEST_RENDER_MAX_SESSIONS",
        "32",
    )
)

SESSION_ROOT.mkdir(parents=True, exist_ok=True)


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


class RenderSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, RenderSession] = {}
        self._lock = threading.RLock()

    async def create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
    ) -> RenderSession:
        session_id = uuid.uuid4().hex
        session_dir = SESSION_ROOT / session_id
        session_dir.mkdir(mode=0o700, parents=True)
        target_path = session_dir / "source.pdf"
        published = False
        try:
            # Unpublished candidates are owned by this invocation. Expiry,
            # deletion and eviction cannot remove them during verification.
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
                # No await between publication and return. A cancelled create
                # cannot publish later from an abandoned background thread.
                self._sessions[session_id] = session
                published = True
                return session
        finally:
            if not published:
                shutil.rmtree(session_dir, ignore_errors=True)

    async def get_or_create(
            self,
            file_path: Path,
            file_size: int,
            sha256: str,
            dedup_scope: str = "global",
    ) -> RenderSession:
        existing = self.find_by_hash(sha256, dedup_scope)
        if existing is not None:
            return existing

        return await self.create(
            file_path=file_path,
            file_size=file_size,
            sha256=sha256,
            dedup_scope=dedup_scope,
        )

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

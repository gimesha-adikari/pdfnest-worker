"""Unprivileged preview guardian with stable child handles and writer drainage.

This does not activate the execution-domain controller. Only its existing Linux
pidfd and adopted-child reaper helpers are reused.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from app.controller.linux import pidfd_open, pidfd_signal, require_child
from app.controller.monitor import reap

ROOT_LOCK_FD_ENV = "PDFNEST_RENDER_ROOT_LOCK_FD"
CANCEL_FD_ENV = "PDFNEST_RENDER_CANCEL_FD"
DRAIN_FD_ENV = "PDFNEST_RENDER_DRAIN_FD"
DRAIN_MARKER_ENV = "PDFNEST_RENDER_DRAIN_MARKER"


class PreviewProcessControl:
    """Parent-owned cancellation and trusted post-reap receipt descriptors."""

    def __init__(self, root_fd, drain_marker=None):
        if drain_marker is not None:
            (Path(drain_marker).parent / "writer.active").touch(exist_ok=False)
        self.cancel_read, self.cancel_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            self.drain_read, self.drain_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        except BaseException:
            os.close(self.cancel_read)
            os.close(self.cancel_write)
            raise
        self.root_fd = root_fd
        self.started = False
        self.verified = False
        self.drain_marker = drain_marker

    @property
    def pass_fds(self):
        return self.root_fd, self.cancel_read, self.drain_write

    def environment(self, environment):
        selected = {**environment, ROOT_LOCK_FD_ENV: str(self.root_fd),
                CANCEL_FD_ENV: str(self.cancel_read), DRAIN_FD_ENV: str(self.drain_write)}
        if self.drain_marker is not None:
            selected[DRAIN_MARKER_ENV] = str(self.drain_marker)
        return selected

    def cancel(self):
        if self.cancel_write is not None:
            os.close(self.cancel_write)
            self.cancel_write = None

    def verify(self):
        if not self.started or self.verified:
            return
        try:
            drained = os.read(self.drain_read, 9) == b"DRAINED\n"
        except BlockingIOError:
            drained = False
        if not drained:
            from .session import quarantine_render_root
            quarantine_render_root()
            raise RuntimeError("preview writer drainage is unverified; render storage is quarantined")
        self.verified = True

    def close(self):
        for name in ("cancel_read", "cancel_write", "drain_read", "drain_write"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)


def supervise(command, *, owner_pid, root_lock_fd, cancel_fd, drain_fd=None, wall_timeout=30):
    probe = pidfd_open(os.getpid())
    try:
        pidfd_signal(probe, 0)
    finally:
        os.close(probe)
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("preview child reaping is unavailable")
    # Validate before spawning. The open flock description remains held by the
    # guardian and native writer even if the API process dies.
    if root_lock_fd is not None:
        os.fstat(root_lock_fd)
    if cancel_fd is not None:
        os.fstat(cancel_fd)
    child = None
    child_fd = None
    deadline = time.monotonic() + min(30, wall_timeout)
    try:
        if os.getppid() != owner_pid:
            return 125
        child = subprocess.Popen(command, pass_fds=(() if root_lock_fd is None else (root_lock_fd,)))
        # Popen has not reaped this child. Even an already-exited child retains
        # its numeric identity here; the pidfd is then checked as our child.
        child_fd = pidfd_open(child.pid)
        require_child(child_fd)
        while True:
            cancelled = cancel_fd is not None and select.select([cancel_fd], [], [], 0)[0]
            if cancelled:
                cancelled = os.read(cancel_fd, 1) == b""
            if cancelled or os.getppid() != owner_pid or time.monotonic() >= deadline:
                return 125
            code = child.poll()
            if code is not None:
                return code
            time.sleep(.01)
    finally:
        if child_fd is not None:
            try:
                pidfd_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(child_fd)
        if child is not None:
            if child_fd is not None:
                child.wait()
            # ECHILD is the receipt. All adopted children, including setsid
            # descendants, are signalled only through verified child pidfds.
            reap(time.monotonic() + 2)
        if drain_fd is not None:
            marker = os.environ.get(DRAIN_MARKER_ENV)
            if marker is not None:
                Path(marker).touch(exist_ok=False)
            os.write(drain_fd, b"DRAINED\n")


def main():
    return supervise(sys.argv[2:], owner_pid=int(sys.argv[1]),
                     root_lock_fd=int(os.environ[ROOT_LOCK_FD_ENV]),
                     cancel_fd=int(os.environ[CANCEL_FD_ENV]),
                     drain_fd=int(os.environ[DRAIN_FD_ENV]))


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import logging
import locale
import os
import selectors
import signal
import subprocess
import time
from typing import Callable

from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def kill_process_group(pgid: int, term_grace_seconds: float = 1.0) -> None:
    """Terminate a process group so child processes do not outlive the job."""
    if pgid <= 1:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
        logger.info("[FORENSIC %s] SIGTERM Sent to process group %d", datetime.now(timezone.utc).isoformat(), pgid)
        logger.info("[SUBPROCESS HARDENING] Sent SIGTERM to process group %d", pgid)
    except (ProcessLookupError, PermissionError, OSError):
        return

    deadline = time.monotonic() + term_grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
            time.sleep(0.05)
        except (ProcessLookupError, OSError):
            logger.info("[FORENSIC %s] Tesseract Exited cleanly on SIGTERM (PGID: %d)", datetime.now(timezone.utc).isoformat(), pgid)
            logger.info("[SUBPROCESS HARDENING] Process group %d exited cleanly on SIGTERM", pgid)
            return

    try:
        os.killpg(pgid, signal.SIGKILL)
        logger.warning("[FORENSIC %s] SIGKILL Sent (if needed) to process group %d", datetime.now(timezone.utc).isoformat(), pgid)
        logger.warning("[SUBPROCESS HARDENING] Sent SIGKILL to process group %d after grace timeout", pgid)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_hardened_subprocess(
    cmd: list[str],
    *,
    timeout: float = 300.0,
    cancellation_check: Callable[[], None] | None = None,
    term_grace_seconds: float = 1.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 16 * 1024 * 1024,
    cleanup_group_on_exit: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run with bounded output capture, cancellation and process-tree cleanup."""
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    # Nested OCR helpers stay in the supervised document group. A local
    # helper timeout kills only that helper; the outer supervisor owns the tree.
    from app.core.native_isolation import IN_CHILD
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=not IN_CHILD,
    )

    pgid = proc.pid  # start_new_session makes the child's PID its process group.
    deadline = time.monotonic() + timeout
    output = {"stdout": bytearray(), "stderr": bytearray()}
    encoding = locale.getpreferredencoding(False)
    selector = selectors.DefaultSelector()
    try:
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)

        # Drain while the child runs: waiting for exit first deadlocks once
        # either pipe fills. Reads and retained output are bounded separately.
        while selector.get_map() or proc.poll() is None:
            if cleanup_group_on_exit and proc.poll() is not None:
                # A descendant may hold an inherited pipe open after the
                # supervisor crashes. Stop it now rather than waiting for EOF.
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if cancellation_check is not None:
                cancellation_check()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd=cmd, timeout=timeout,
                    output=output["stdout"].decode(encoding, errors="replace"),
                    stderr=output["stderr"].decode(encoding, errors="replace"),
                )
            for key, _ in selector.select(timeout=min(0.05, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured = output[key.data]
                if len(captured) + len(chunk) > max_output_bytes:
                    raise RuntimeError(f"Subprocess {key.data} exceeded {max_output_bytes} bytes")
                captured.extend(chunk)
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.wait(),
            stdout=output["stdout"].decode(encoding, errors="replace"),
            stderr=output["stderr"].decode(encoding, errors="replace"),
        )
    except BaseException:
        if IN_CHILD:
            proc.kill()
        else:
            kill_process_group(pgid, term_grace_seconds=term_grace_seconds)
        proc.wait()
        raise
    finally:
        if cleanup_group_on_exit and not IN_CHILD:
            # Clean up descendants even if the document child exited first.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        selector.close()
        proc.stdout.close()
        proc.stderr.close()

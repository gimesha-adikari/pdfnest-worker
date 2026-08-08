from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Callable

logger = logging.getLogger(__name__)


def kill_process_group(pgid: int, term_grace_seconds: float = 1.0) -> None:
    """Safely terminates an entire Linux process group using SIGTERM -> grace -> SIGKILL."""
    if pgid <= 1:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
        logger.info("[SUBPROCESS HARDENING] Sent SIGTERM to process group %d", pgid)
    except (ProcessLookupError, PermissionError, OSError):
        return

    deadline = time.time() + term_grace_seconds
    while time.time() < deadline:
        try:
            # Check if process group is still alive
            os.killpg(pgid, 0)
            time.sleep(0.05)
        except (ProcessLookupError, OSError):
            logger.info("[SUBPROCESS HARDENING] Process group %d exited cleanly on SIGTERM", pgid)
            return

    try:
        os.killpg(pgid, signal.SIGKILL)
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
) -> subprocess.CompletedProcess[str]:
    """
    Executes an external CLI command inside an independent Linux process group (`start_new_session=True`).
    Supports cooperative mid-execution cancellation and process-group SIGTERM -> SIGKILL cleanup.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )

    pgid = os.getpgid(proc.pid)
    deadline = time.time() + timeout

    while True:
        retcode = proc.poll()
        if retcode is not None:
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=retcode,
                stdout=stdout,
                stderr=stderr,
            )

        # 1. Cooperative Cancellation Check mid-execution
        if cancellation_check is not None:
            try:
                cancellation_check()
            except Exception as exc:
                logger.info("[SUBPROCESS HARDENING] Cancellation triggered during execution of %s (PGID: %d)", cmd[0], pgid)
                kill_process_group(pgid, term_grace_seconds=term_grace_seconds)
                proc.communicate()
                raise exc

        # 2. Timeout Check
        if time.time() > deadline:
            logger.warning("[SUBPROCESS HARDENING] Timeout (%fs) exceeded for %s (PGID: %d)", timeout, cmd[0], pgid)
            kill_process_group(pgid, term_grace_seconds=term_grace_seconds)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output=stdout, stderr=stderr)

        time.sleep(0.1)

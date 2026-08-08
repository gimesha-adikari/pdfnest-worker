from __future__ import annotations

import pytest
import subprocess
from app.core.subprocess_runner import run_hardened_subprocess
from app.jobs.cancellation import JobCancelledException


def test_run_hardened_subprocess_success() -> None:
    res = run_hardened_subprocess(["echo", "hello world"], timeout=5.0)
    assert res.returncode == 0
    assert "hello world" in res.stdout


def test_run_hardened_subprocess_timeout_kills_process_group() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_hardened_subprocess(["sleep", "10"], timeout=0.2, term_grace_seconds=0.1)


def test_run_hardened_subprocess_cancellation_kills_process_group() -> None:
    call_count = 0

    def mock_cancellation_check():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise JobCancelledException("Cancelled mid-execution")

    with pytest.raises(JobCancelledException):
        run_hardened_subprocess(
            ["sleep", "10"],
            timeout=5.0,
            cancellation_check=mock_cancellation_check,
            term_grace_seconds=0.1,
        )

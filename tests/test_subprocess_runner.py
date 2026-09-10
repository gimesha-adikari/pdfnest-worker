from __future__ import annotations

import pytest
import subprocess
import sys
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


def test_run_hardened_subprocess_drains_both_output_pipes() -> None:
    size = 256 * 1024
    result = run_hardened_subprocess(
        [sys.executable, "-c", f"import sys; sys.stdout.write('o' * {size}); sys.stderr.write('e' * {size})"],
        timeout=2.0,
        term_grace_seconds=0.05,
    )
    assert result.returncode == 0
    assert result.stdout == "o" * size
    assert result.stderr == "e" * size


def test_run_hardened_subprocess_bounds_output() -> None:
    with pytest.raises(RuntimeError, match="stdout exceeded"):
        run_hardened_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 262144)"],
            timeout=2.0, max_output_bytes=32768, term_grace_seconds=0.05,
        )


def test_run_hardened_subprocess_preserves_nonzero_exit() -> None:
    result = run_hardened_subprocess([sys.executable, "-c", "import sys; print('failed', file=sys.stderr); sys.exit(7)"])
    assert result.returncode == 7
    assert result.stderr == "failed\n"

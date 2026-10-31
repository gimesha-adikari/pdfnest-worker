"""Bounded ordinary-UID subprocess fixtures; no manager/UID/cgroup provisioning."""
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import time
import pytest


def module():
    assert importlib.util.find_spec('app.controller.monitor') is not None, 'trusted monitor source is missing'
    from app.controller import monitor
    return monitor


def test_capture_rejects_oversized_native_stream_and_reaps_direct_child():
    m = module()
    child = subprocess.Popen([sys.executable, '-c', "import sys;sys.stdout.write('x'*8192)"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    with pytest.raises(RuntimeError, match='CAPTURE_LIMIT'):
        m.capture(child, time.monotonic() + 2, 1024)
    assert child.poll() is not None


def test_monitor_never_runs_native_without_separate_root_authority(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    with pytest.raises(RuntimeError, match='AUTHORITY_UNAVAILABLE'):
        m.main(tmp_path / 'missing.json')


def test_subreaper_drains_detached_adopted_descendant_in_bounded_fixture():
    module()
    code = '''
import ctypes, os, time
from app.controller.monitor import reap
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
pid = os.fork()
if pid == 0:
    grandchild = os.fork()
    if grandchild == 0:
        os.setsid()
        time.sleep(2)
        os._exit(0)
    os._exit(0)
os.waitpid(pid, 0)
reap(time.monotonic() + 1)
try:
    os.waitpid(-1, os.WNOHANG)
except ChildProcessError:
    print('REAPED')
else:
    raise AssertionError('descendant remains')
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=4)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'REAPED'

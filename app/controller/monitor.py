"""Fixed privileged per-domain gate/reaper. Imports only the standard library.

The system manager must place this monitor in the charged namespace/profile
before it starts. Only the post-gate separate-UID child imports native modules.
No manager descriptor or control socket reaches the child.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from app.controller.linux import pidfd_open, pidfd_signal, require_child


def capture(child, deadline, limit):
    streams = {child.stdout: bytearray(), child.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise RuntimeError('NATIVE_DEADLINE')
            for key, _ in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                data = os.read(key.fileobj.fileno(), min(65536, limit + 1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(data)
                if len(streams[key.fileobj]) > limit:
                    raise RuntimeError('CAPTURE_LIMIT')
        child.wait(timeout=max(.001, deadline - time.monotonic()))
        return child.returncode, *(bytes(streams[s]).decode('utf-8', errors='replace') for s in (child.stdout, child.stderr))
    except BaseException:
        child.kill()
        child.wait(timeout=1)
        raise
    finally:
        selector.close()
        for stream in streams:
            stream.close()


def reap(deadline):
    """Drain children adopted by PR_SET_CHILD_SUBREAPER, including setsid forks.

    Numeric IDs from proc are never used to signal: pidfds must first pass
    waitid's child relationship check. ECHILD certifies responsible reap.
    """
    while time.monotonic() < deadline:
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            return
        children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
        for number in children:
            fd = None
            try:
                fd = pidfd_open(int(number))
                require_child(fd)
                pidfd_signal(fd, signal.SIGKILL)
            except (ProcessLookupError, ChildProcessError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        time.sleep(.01)
    raise RuntimeError('DRAIN_UNVERIFIED')


def _protected_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise RuntimeError('SPEC_REJECTED')
        return json.loads(stream.read(8193))


def main(path=Path('/control/spec.json')):
    if os.geteuid() != 0:
        raise RuntimeError('AUTHORITY_UNAVAILABLE')
    spec = _protected_json(path)
    if (set(spec) != {'domain', 'token', 'uid', 'gid', 'wall_seconds', 'output_limit', 'input_sha256'}
            or re.fullmatch('native-[0-9a-f]{64}', spec['domain']) is None
            or re.fullmatch('[0-9a-f]{64}', spec['token']) is None
            or type(spec['uid']) is not int or spec['uid'] <= 0
            or type(spec['gid']) is not int or spec['gid'] <= 0
            or type(spec['wall_seconds']) not in (int, float) or not 0 < spec['wall_seconds'] <= 30
            or spec['output_limit'] != 1024):
        raise RuntimeError('SPEC_REJECTED')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER.
        raise RuntimeError('REAPER_UNAVAILABLE')
    directory = Path(path).parent
    start = time.monotonic()
    while True:
        if time.monotonic() - start >= 5:
            raise RuntimeError('GATE_DEADLINE')
        grant = directory / 'grant'
        if grant.exists():
            fd = os.open(grant, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_mode & 0o077 or stream.read(65) != spec['token'].encode()):
                    raise RuntimeError('GATE_REJECTED')
            break
        time.sleep(.01)
    # A finalized immutable snapshot is the only native-visible input.
    fd = os.open('/input.pdf', os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or not 0 < info.st_size <= 16 * 1024 * 1024:
            raise RuntimeError('INPUT_REJECTED')
        digest = hashlib.sha256(stream.read(16 * 1024 * 1024 + 1)).hexdigest()
    if digest != spec['input_sha256']:
        raise RuntimeError('INPUT_REJECTED')

    def drop():
        os.setgroups([])
        os.setresgid(spec['gid'], spec['gid'], spec['gid'])
        os.setresuid(spec['uid'], spec['uid'], spec['uid'])
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS.
            os._exit(125)
        libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, disable cross-process debugging.

    child = None
    try:
        # The monitor is the guardian's actual parent in the same PID namespace.
        command = ['/usr/bin/python3', '-I', '-m', 'app.api.tools.render.session_metadata',
                   '/input.pdf', str(os.getpid())]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                 preexec_fn=drop, cwd='/work', env={
                                     'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin', 'TMPDIR': '/work',
                                     'TMP': '/work', 'TEMP': '/work',
                                     'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
        code, out, err = capture(child, time.monotonic() + spec['wall_seconds'], spec['output_limit'])
        reap(time.monotonic() + 2)
        payload = json.dumps({'returncode': code, 'stdout': out, 'stderr': err, 'reaped': True}).encode()
        temporary = directory / 'complete.pending'
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / 'complete.json')
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=1)
        reap(time.monotonic() + 2)


if __name__ == '__main__':
    try:
        main(Path(sys.argv[1]))
    except BaseException:
        # Systemd/Worker receive only safe failure; no raw privileged exception.
        sys.exit(125)

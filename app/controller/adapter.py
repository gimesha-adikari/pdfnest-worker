"""Worker contract adapter. Explicit trusted startup only; legacy stays default.

Only session metadata currently has a complete fixed schema. Registry/OCR
launches are refused; adding them requires explicit argument and output maps.
No incoming command/environment is forwarded across the protected boundary.
"""
from __future__ import annotations
import hashlib
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from app.core import native_domain as d
from app.controller.policy import MAX_INPUT, METADATA_MODULE


def translate(launch):
    command = launch.command
    if (type(launch) is not d.Launch or len(command) != 5
            or command[:3] != (sys.executable, '-m', METADATA_MODULE)
            or command[4] != str(os.getpid())
            or type(launch.timeout) not in (int, float) or not math.isfinite(launch.timeout)
            or not 0 < launch.timeout <= 30 or launch.max_output_bytes != 1024):
        raise d.DomainUnavailable()
    fd = None
    try:
        # No received path/FD reaches the privileged bridge: only bounded bytes.
        fd = os.open(command[3], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                or not 0 < before.st_size <= MAX_INPUT):
            raise d.DomainUnavailable()
        chunks = bytearray()
        while len(chunks) <= MAX_INPUT:
            data = os.read(fd, min(65536, MAX_INPUT + 1 - len(chunks)))
            if not data:
                break
            chunks.extend(data)
        after = os.fstat(fd)
        if (len(chunks) != before.st_size or len(chunks) > MAX_INPUT
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise d.DomainUnavailable()
        return {'kind': 'render_metadata', 'input': bytes(chunks)}
    except (OSError, ValueError) as exc:
        raise d.DomainUnavailable() from exc
    finally:
        if fd is not None:
            os.close(fd)


class WorkerController:
    def __init__(self, client):
        self.client = client
        self.digests = {}

    def capabilities(self, deadline):
        return self.client.request('capabilities', deadline)

    def acquire(self, operation, deadline, check):
        check()
        # Capacity wait and reply delivery are separate. Zero wait makes exactly
        # one broker allocation decision, with a finite five-second reply budget.
        wait = min(300, max(0, deadline - time.monotonic()))
        result = self.client.request('acquire', deadline + d.CONTROL_SECONDS, operation=operation, wait=wait)
        check()
        return result

    def prepare(self, lease, launch, deadline):
        manifest = translate(launch)
        return self.client.request('prepare', deadline, lease=lease, manifest=manifest)

    def run(self, lease, member, launch, deadline, check):
        check()
        self.client.request('run', min(deadline, time.monotonic() + 5), lease=lease, token=member.token)
        while time.monotonic() < deadline:
            check()
            result = self.client.request('poll', min(deadline, time.monotonic() + 5), lease=lease)
            if result is not None:
                if (type(result) is not dict or set(result) != {'returncode', 'stdout', 'stderr', 'reaped'}
                        or result['reaped'] is not True):
                    raise d.DomainUnavailable()
                return subprocess.CompletedProcess(launch.command, result['returncode'], result['stdout'], result['stderr'])
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
        raise subprocess.TimeoutExpired(launch.command, launch.timeout)

    def drain(self, lease, deadline):
        receipt, result, digest = self.client.request('drain', deadline, lease=lease)
        import json
        observed = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if observed != digest or type(receipt) is not d.Drain or receipt.lease != lease:
            raise d.DomainUnavailable()
        self.digests[lease.operation] = digest
        return receipt

    def release(self, lease, deadline):
        digest = self.digests[lease.operation]
        response = self.client.request('release', deadline, lease=lease, digest=digest)
        if response is not None:
            raise d.DomainUnavailable()
        self.digests.pop(lease.operation)

    def quarantine(self, operation, deadline):
        return self.client.request('quarantine', deadline, operation=operation)


def trusted_registration(socket_path, *, broker_uid, identity, limits, slots):
    """Called only by reviewed operator startup; never consults environment.

    Returns a pinned Registration for explicit installation by that startup.
    Does not mutate native_domain._registration or enable required mode.
    """
    from app.controller.transport import Client
    if broker_uid in (0, os.geteuid()) or broker_uid <= 0:
        raise d.DomainUnavailable()
    controller = WorkerController(Client(Path(socket_path), broker_uid, identity))
    caps = controller.capabilities(time.monotonic() + d.CONTROL_SECONDS)
    expected = d.Capabilities(identity, slots, limits, True, True, True, True, True, True, True)
    if caps != expected:
        controller.client.close()
        raise d.DomainUnavailable()
    return d.Registration(controller, identity, limits, slots)

"""Dormant execution-domain contract. No production/runtime adapter is shipped.

Only trusted startup code may supply a Registration after independently verifying
its adapter/control plane. Environment/request data never selects an adapter.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
import uuid


class DomainUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('Native execution domain is unavailable')


class DomainBusy(DomainUnavailable):
    pass


@dataclass(frozen=True)
class Identity:
    name: str
    epoch: str
    policy_sha256: str


@dataclass(frozen=True)
class Limits:
    memory_bytes: int
    swap_bytes: int
    tasks: int
    cpu_quota: int
    cpu_period: int
    wall_seconds: float


@dataclass(frozen=True)
class Capabilities:
    identity: Identity
    slots: tuple[str, ...]
    limits: Limits
    independent: bool
    gated: bool
    protected: bool
    recovery_verified: bool
    bounded: bool
    domain_kill: bool = False
    accounted: bool = False


@dataclass(frozen=True)
class Lease:
    identity: Identity
    operation: str
    slot: str
    generation: int
    domain: str


@dataclass(frozen=True)
class Membership:
    lease: Lease
    limits: Limits
    token: str
    native_started: bool


@dataclass(frozen=True)
class Drain:
    lease: Lease
    empty: bool
    reaped: bool
    reconciled: bool


@dataclass(frozen=True)
class Launch:
    command: tuple[str, ...]
    env: Mapping[str, str]
    timeout: float
    max_output_bytes: int


class Controller(Protocol):
    """All calls bounded by absolute monotonic deadlines, enforced by adapter.

    prepare creates a gated member, never native execution. run grants that
    verified gate, captures bounded output and polls check. drain terminates
    ALL descendants even after success. release is allowed only after verified
    drainage/validation/cleanup. quarantine must survive owner/controller death;
    capabilities must refuse unreconciled prior epochs. These obligations need
    an independent operator-owned runtime; Python assertions cannot supply one.
    """
    def capabilities(self, deadline: float) -> Capabilities: ...
    def acquire(self, operation: str, deadline: float, check: Callable) -> Lease: ...
    def prepare(self, lease: Lease, launch: Launch, deadline: float) -> Membership: ...
    def run(self, lease: Lease, member: Membership, launch: Launch,
            deadline: float, check: Callable) -> subprocess.CompletedProcess: ...
    def drain(self, lease: Lease, deadline: float) -> Drain: ...
    def release(self, lease: Lease, deadline: float) -> None: ...
    def quarantine(self, operation: str, deadline: float) -> None: ...


@dataclass
class Registration:
    controller: Controller
    expected: Identity
    expected_limits: Limits | None = None
    expected_slots: tuple[str, ...] = ()
    blocked: bool = False
    seen: dict[str, tuple[int, str]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    verified: Capabilities | None = None
    active: dict[str, str] = field(default_factory=dict)


# Deliberately no manager socket, plugin loader, fake-controller flag or runtime
# registration API. Tests replace this private value; deployment wiring is absent.
_registration: Registration | None = None
CONTROL_SECONDS = 5.0
DRAIN_SECONDS = 5.0


def _tag(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) is not None


def _limits(value):
    return (type(value) is Limits
            and all(type(n) is int and 0 < n < 2**63 for n in (
                value.memory_bytes, value.tasks, value.cpu_quota, value.cpu_period))
            and type(value.swap_bytes) is int and 0 <= value.swap_bytes < 2**63
            and type(value.wall_seconds) in (int, float)
            and math.isfinite(value.wall_seconds) and 0 < value.wall_seconds <= 300)


class Operation:
    def __init__(self, registration=None, check=lambda: None, wait=0):
        self.registration = registration
        self.required = registration is not None
        self.check = check
        self.lease = None
        self.operation_id = None
        self.drained = False
        self.drain_attempted = False
        self.cleaned = False
        self.started = False
        self.state = 'LEASED' if self.required else 'LEGACY'
        if not self.required:
            return
        r = registration
        with r.lock:
            if r.blocked:
                raise DomainUnavailable()
        callback_error = None
        def admission_check():
            nonlocal callback_error
            try:
                check()
            except BaseException as exc:
                callback_error = exc
                raise
        try:
            caps = r.controller.capabilities(time.monotonic() + CONTROL_SECONDS)
            identity = r.expected
            if (type(identity) is not Identity or not _tag(identity.name) or not _tag(identity.epoch)
                    or re.fullmatch('[0-9a-f]{64}', identity.policy_sha256) is None
                    or type(caps) is not Capabilities or caps.identity != identity
                    or not _limits(caps.limits) or caps.limits != r.expected_limits
                    or caps.slots != r.expected_slots or type(caps.slots) is not tuple
                    or not 0 < len(caps.slots) <= 64 or len(set(caps.slots)) != len(caps.slots)
                    or not all(_tag(s) for s in caps.slots)
                    or not all(v is True for v in (caps.independent, caps.gated, caps.protected,
                                                   caps.recovery_verified, caps.bounded,
                                                   caps.domain_kill, caps.accounted))):
                raise DomainUnavailable()
            self.caps = caps
            with r.lock:
                if r.verified is not None and caps != r.verified:
                    raise DomainUnavailable()
                r.verified = caps
            operation = uuid.uuid4().hex
            self.operation_id = operation
            self.lease = r.controller.acquire(operation, time.monotonic() + wait, admission_check)
            lease = self.lease
            if (type(lease) is not Lease or lease.identity != identity or lease.operation != operation
                    or lease.slot not in caps.slots or not _tag(lease.domain)
                    or type(lease.generation) is not int or not 0 < lease.generation < 2**63):
                raise DomainUnavailable()
            with r.lock:
                previous = r.seen.get(lease.slot, (0, ''))
                if (r.blocked or lease.slot in r.active or lease.generation <= previous[0]
                        or any(lease.domain == old_domain for _, old_domain in r.seen.values())):
                    raise DomainUnavailable()
                r.seen[lease.slot] = (lease.generation, lease.domain)
                r.active[lease.slot] = lease.operation
        except DomainBusy:
            raise
        except BaseException as exc:
            self.quarantine()
            if exc is callback_error or isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise DomainUnavailable() from exc

    def quarantine(self):
        if not self.required:
            return
        r = self.registration
        self.state = 'QUARANTINED'
        with r.lock:
            r.blocked = True  # No local reset or time-only recovery.
        if self.operation_id is not None:
            try:
                r.controller.quarantine(self.operation_id, time.monotonic() + CONTROL_SECONDS)
            except BaseException:
                # Remains blocked even if the controller is gone. Independent
                # deadline/recovery is an adapter obligation, not a flock close.
                pass

    def run(self, command, *, timeout, cancellation_check=None, env=None,
            max_output_bytes=65536, legacy_runner=None, **legacy_options):
        if not self.required:
            return legacy_runner(command, timeout=timeout, cancellation_check=cancellation_check,
                                 env=env, max_output_bytes=max_output_bytes, **legacy_options)
        if self.started or not math.isfinite(timeout) or not 0 < timeout <= self.caps.limits.wall_seconds:
            raise DomainUnavailable()
        r = self.registration
        callback_error = None
        def check():
            nonlocal callback_error
            try:
                with r.lock:
                    if r.blocked:
                        raise DomainUnavailable()
                self.check()
                if cancellation_check is not None:
                    cancellation_check()
            except BaseException as exc:
                callback_error = exc
                raise
        launch_env = {k: v for k, v in (env or {}).items()
                      if k not in ('DBUS_SESSION_BUS_ADDRESS', 'DBUS_SYSTEM_BUS_ADDRESS', 'XDG_RUNTIME_DIR')
                      and not k.startswith('NATIVE_EXECUTION_DOMAIN_')}
        launch = Launch(tuple(command), MappingProxyType(launch_env), timeout, max_output_bytes)
        try:
            check()
            member = r.controller.prepare(self.lease, launch, time.monotonic() + CONTROL_SECONDS)
            if (type(member) is not Membership or member.lease != self.lease
                    or member.limits != self.caps.limits or member.native_started is not False
                    or not _tag(member.token)):
                raise DomainUnavailable()
            check()
            self.started = True
            self.state = 'RUNNING'
            result = r.controller.run(self.lease, member, launch, time.monotonic() + timeout, check)
            if (not isinstance(result, subprocess.CompletedProcess) or type(result.returncode) is not int
                    or type(result.stdout) is not str or type(result.stderr) is not str
                    or any(len(s.encode('utf-8')) > max_output_bytes for s in (result.stdout, result.stderr))):
                raise DomainUnavailable()
            return result
        except BaseException as exc:
            if exc is callback_error or isinstance(exc, (KeyboardInterrupt, SystemExit, subprocess.TimeoutExpired)):
                raise
            raise DomainUnavailable() from exc

    def drain(self):
        if not self.required or self.drained:
            return
        if self.drain_attempted:
            raise DomainUnavailable()
        self.drain_attempted = True
        try:
            self.state = 'DRAINING'
            receipt = self.registration.controller.drain(self.lease, time.monotonic() + DRAIN_SECONDS)
            if (type(receipt) is not Drain or receipt.lease != self.lease
                    or not all(v is True for v in (receipt.empty, receipt.reaped, receipt.reconciled))):
                raise DomainUnavailable()
            self.drained = True
        except BaseException as exc:
            self.quarantine()
            raise DomainUnavailable() from exc

    def cleanup_complete(self):
        if self.required and (not self.drained or not self.started):
            raise DomainUnavailable()
        self.cleaned = True

    def finish(self, failed):
        if not self.required:
            return
        try:
            self.drain()
            if failed or not self.cleaned or not self.started:
                raise DomainUnavailable()
            with self.registration.lock:
                if self.registration.blocked:
                    raise DomainUnavailable()
            self.registration.controller.release(self.lease, time.monotonic() + CONTROL_SECONDS)
            with self.registration.lock:
                if self.registration.active.get(self.lease.slot) != self.lease.operation:
                    raise DomainUnavailable()
                del self.registration.active[self.lease.slot]
            self.state = 'FREE'
        except BaseException:
            self.quarantine()
            if not failed:
                raise DomainUnavailable() from None


@contextmanager
def admit(check, wait, legacy_capacity):
    mode = os.environ.get('NATIVE_EXECUTION_DOMAIN_MODE', 'legacy')
    if mode == 'legacy':
        with legacy_capacity():
            yield Operation()
        return
    if mode != 'required-nonproduction' or _registration is None:
        raise DomainUnavailable()
    operation = Operation(_registration, check, wait)
    failed = True
    try:
        yield operation
        failed = False
    finally:
        operation.finish(failed)


@contextmanager
def staged_directory(domain, prefix):
    """Do not remove input/output staging while a hard domain may still live.

    A failed drain deliberately retains the private directory for quarantine;
    the blocked registration prevents repeated admission/staging accumulation.
    There is no automatic filesystem or generation recovery in this candidate.
    """
    if not domain.required:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            yield directory
        return
    directory = tempfile.mkdtemp(prefix=prefix)
    try:
        yield directory
    finally:
        domain.drain()  # Before deletion, including cancellation/exception paths.
        try:
            shutil.rmtree(directory)
        except BaseException as exc:
            domain.quarantine()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise DomainUnavailable() from exc

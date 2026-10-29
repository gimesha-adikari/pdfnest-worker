"""Dormant execution-domain contract; operator adapter candidate is unactivated.

Only trusted startup code may supply a Registration after independently verifying
its adapter/control plane. Environment/request data never selects an adapter.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
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
class BusyReceipt:
    """Trusted controller's atomic refusal: no state changed for this nonce.

    The type/binding checks are not proof of runtime atomicity. Only an
    independently verified controller can establish this assertion's truth.
    A bare exception, including DomainBusy, makes no such assertion.
    """
    identity: Identity
    operation: str


def domain_identity(identity: Identity, operation: str, slot: str, generation: int) -> str:
    """Canonical acquisition binding, not a capability or kernel attestation.

    Identity/slot tags and the fresh nonce are validated by the caller. The
    controller must durably enforce uniqueness and reconcile previous epochs.
    """
    fields = (identity.name, identity.epoch, identity.policy_sha256, operation, slot, str(generation))
    return 'native-' + hashlib.sha256('\n'.join(fields).encode('ascii')).hexdigest()


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
    # BusyReceipt must be an atomic zero-side-effect decision for this nonce.
    # Any interrupted/exceptional acquisition is indeterminate and quarantined.
    def acquire(self, operation: str, deadline: float, check: Callable) -> Lease | BusyReceipt: ...
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
    seen: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    verified: Capabilities | None = None
    active: dict[str, str] = field(default_factory=dict)
    releasing: dict[str, Lease] = field(default_factory=dict)
    condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self):
        self.condition = threading.Condition(self.lock)


# Environment/request data cannot install an adapter. Reviewed operator startup
# may explicitly supply app.controller.adapter.trusted_registration; no application
# startup invokes it. Tests replace this private value. Legacy remains default.
_registration: Registration | None = None
CONTROL_SECONDS = 5.0
DRAIN_SECONDS = 5.0
HANDOFF_SECONDS = 5.0
HANDOFF_POLL_SECONDS = 0.05  # Poll callable cancellation while condition-waiting.

# Exact names only: OCR_* and other prefixes can also contain service secrets.
# The independent controller must still pin runtime paths and reconstruct its
# own immutable runtime environment. These client hints grant no authority.
_NATIVE_ENVIRONMENT = frozenset({
    'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
    'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'OCR_PAGE_WORKERS',
    'GLOBAL_TESSERACT_CAPACITY', 'TESSDATA_PREFIX', 'PPOCRV6_MODEL_ROOT',
    'OCR_TEXT_ENGINE', 'SEARCHABLE_PDF_ENGINE', 'DOCUMENT_EXTRACTION_ENGINE',
    'PDF_TO_MARKDOWN_ENGINE', 'PDF_TO_WORD_OCR_ENGINE', 'OCR_MARKUP_ENGINE',
    'EDITOR_OCR_ENGINE', 'STUDIO_EDITOR_EXTRACTION_ENGINE',
    'STUDIO_MARKUP_REGION_OCR_ENGINE', 'LEGACY_EDITOR_OCR_ENGINE',
    'LEGACY_MARKUP_OCR_ENGINE', 'OCR_AUTO_INITIAL_PROBES', 'OCR_MAX_AUTO_PROBES',
    'OCR_AUTO_EXPANDED_MAX_PROBES', 'OCR_AUTO_MIN_CONFIDENCE',
    'OCR_MAX_AUTO_CANDIDATES', 'OCR_V2_MAX_IMAGE_PIXELS', 'OCR_V2_MAX_PAGES',
    'OCR_V2_STRUCTURED_MAX_BYTES', 'OCR_V2_STRUCTURED_MAX_OUTPUT_BYTES',
    'OCR_V2_STRUCTURED_MAX_RASTER_PIXELS', 'OCR_V2_STRUCTURED_PAGE_TIMEOUT_SECONDS',
})


def _before_deadline(deadline):
    # Reject late receipts; this cannot interrupt a hung Python adapter.
    # Bounded I/O and an independent watchdog remain controller obligations.
    if time.monotonic() >= deadline:
        raise DomainUnavailable()


def _launch(command, env, timeout, max_output_bytes):
    if (type(command) not in (list, tuple) or not 0 < len(command) <= 64
            or any(type(s) is not str or not s or '\x00' in s or len(s) > 4096 for s in command)
            or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 65536
            or (env is not None and not isinstance(env, Mapping))):
        raise DomainUnavailable()
    selected = {}
    for name, value in (env or {}).items():
        if name not in _NATIVE_ENVIRONMENT:
            continue
        if type(value) is not str or '\x00' in value or len(value.encode('utf-8')) > 4096:
            raise DomainUnavailable()
        selected[name] = value
    return Launch(tuple(command), MappingProxyType(selected), timeout, max_output_bytes)


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
        self.controller = registration.controller if registration is not None else None
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
            if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 300:
                raise DomainUnavailable()
            capability_deadline = time.monotonic() + CONTROL_SECONDS
            caps = self.controller.capabilities(capability_deadline)
            _before_deadline(capability_deadline)
            admission_check()
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
            admission_deadline = time.monotonic() + wait
            result = self.controller.acquire(operation, admission_deadline, admission_check)
            # Zero admission wait is used by metadata. Allow only the finite
            # control-I/O budget for delivering its atomic immediate outcome.
            _before_deadline(admission_deadline + CONTROL_SECONDS)
            admission_check()
            busy = type(result) is BusyReceipt
            if busy:
                if (type(result.identity) is not Identity or result.identity != identity
                        or type(result.operation) is not str or result.operation != operation):
                    raise DomainUnavailable()
                with r.lock:
                    if r.blocked or not self._policy_current():
                        raise DomainUnavailable()
            else:
                self.lease = result
                lease = self.lease
                if (type(lease) is not Lease or type(lease.identity) is not Identity
                        or lease.identity != identity or type(lease.operation) is not str
                        or lease.operation != operation or type(lease.slot) is not str
                        or lease.slot not in caps.slots or type(lease.domain) is not str
                        or type(lease.generation) is not int or not 0 < lease.generation < 2**63
                        or lease.domain != domain_identity(identity, operation, lease.slot, lease.generation)):
                    raise DomainUnavailable()
                self._claim_lease(admission_check)
        except BaseException as exc:
            self.quarantine()
            if exc is callback_error or isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise DomainUnavailable() from exc
        # Only a validated return value reaches this branch. An exception of
        # the same name from acquire is uncertain and follows quarantine above.
        if busy:
            raise DomainBusy()

    def _policy_current(self):
        """Local pinned state only; never call controller code under the lock."""
        r = self.registration
        return (r.controller is self.controller and r.expected == self.caps.identity
                and r.expected_limits == self.caps.limits and r.expected_slots == self.caps.slots
                and r.verified == self.caps)

    def _claim_lease(self, check):
        r, lease = self.registration, self.lease
        deadline = time.monotonic() + HANDOFF_SECONDS
        waited, reverified = False, False
        while True:
            check()  # Callbacks may re-enter registration; no lock held here.
            if waited and time.monotonic() >= deadline:
                raise DomainUnavailable()
            with r.condition:
                previous = r.seen.get(lease.slot, 0)
                if r.blocked or not self._policy_current() or lease.generation <= previous:
                    raise DomainUnavailable()
                owner = r.active.get(lease.slot)
                if owner is None:
                    if lease.slot in r.releasing:
                        raise DomainUnavailable()
                    if not waited or reverified:
                        r.seen[lease.slot] = lease.generation
                        r.active[lease.slot] = lease.operation
                        return
                else:
                    prior = r.releasing.get(lease.slot)
                    if (type(prior) is not Lease or prior.operation != owner
                            or prior.identity != self.caps.identity or prior.slot != lease.slot
                            or prior.generation != previous or prior.domain != domain_identity(
                                self.caps.identity, owner, lease.slot, previous)):
                        # RUNNING/DRAINING overlap or inconsistent local state.
                        raise DomainUnavailable()
                    waited = True
                    reverified = False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DomainUnavailable()
                    r.condition.wait(min(remaining, HANDOFF_POLL_SECONDS))
                    continue
            # A release completed. Revalidate the pinned controller/epoch before
            # claiming, then recheck local ownership/generation under the lock.
            capability_deadline = min(deadline, time.monotonic() + CONTROL_SECONDS)
            caps = self.controller.capabilities(capability_deadline)
            _before_deadline(capability_deadline)
            if type(caps) is not Capabilities or caps != self.caps:
                raise DomainUnavailable()
            reverified = True

    def quarantine(self):
        if not self.required:
            return
        r = self.registration
        self.state = 'QUARANTINED'
        with r.condition:
            r.blocked = True  # No local reset or time-only recovery.
            r.condition.notify_all()
        if self.operation_id is not None:
            try:
                self.controller.quarantine(self.operation_id, time.monotonic() + CONTROL_SECONDS)
            except BaseException:
                # Remains blocked even if the controller is gone. Independent
                # deadline/recovery is an adapter obligation, not a flock close.
                pass

    def run(self, command, *, timeout, cancellation_check=None, env=None,
            max_output_bytes=65536, legacy_runner=None, **legacy_options):
        if not self.required:
            return legacy_runner(command, timeout=timeout, cancellation_check=cancellation_check,
                                 env=env, max_output_bytes=max_output_bytes, **legacy_options)
        if (self.started or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= self.caps.limits.wall_seconds):
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
        try:
            launch = _launch(command, env, timeout, max_output_bytes)
            check()
            prepare_deadline = time.monotonic() + CONTROL_SECONDS
            member = self.controller.prepare(self.lease, launch, prepare_deadline)
            _before_deadline(prepare_deadline)
            if (type(member) is not Membership or member.lease != self.lease
                    or member.limits != self.caps.limits or member.native_started is not False
                    or not _tag(member.token)):
                raise DomainUnavailable()
            check()
            self.started = True
            self.state = 'RUNNING'
            run_deadline = time.monotonic() + timeout
            result = self.controller.run(self.lease, member, launch, run_deadline, check)
            check()
            if time.monotonic() >= run_deadline:
                raise subprocess.TimeoutExpired(launch.command, timeout)
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
            drain_deadline = time.monotonic() + DRAIN_SECONDS
            receipt = self.controller.drain(self.lease, drain_deadline)
            _before_deadline(drain_deadline)
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
            r, lease = self.registration, self.lease
            with r.condition:
                if (r.blocked or not self._policy_current() or r.active.get(lease.slot) != lease.operation
                        or r.seen.get(lease.slot) != lease.generation or lease.slot in r.releasing):
                    raise DomainUnavailable()
                r.releasing[lease.slot] = lease
                self.state = 'RELEASING'
            # The trusted adapter must verify authoritative release before
            # returning None. No global registration lock covers this RPC.
            release_deadline = time.monotonic() + CONTROL_SECONDS
            response = self.controller.release(lease, release_deadline)
            if response is not None or time.monotonic() >= release_deadline:
                raise DomainUnavailable()
            with r.condition:
                if (r.blocked or not self._policy_current() or r.active.get(lease.slot) != lease.operation
                        or r.seen.get(lease.slot) != lease.generation or r.releasing.get(lease.slot) != lease):
                    raise DomainUnavailable()
                del r.active[lease.slot]
                del r.releasing[lease.slot]
                self.state = 'FREE'
                r.condition.notify_all()
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

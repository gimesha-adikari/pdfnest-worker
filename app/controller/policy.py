"""Operator-pinned policy. No client command, UID, environment or unit properties."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from app.core.native_domain import Identity, Limits, DomainUnavailable, _limits

MAX_INPUT = 16 * 1024 * 1024
MAX_WIRE = 24 * 1024 * 1024
METADATA_MODULE = 'app.api.tools.render.session_metadata'


@dataclass(frozen=True)
class Policy:
    identity: Identity
    limits: Limits
    slots: tuple[str, ...]
    native_uids: tuple[int, ...]
    client_uids: tuple[int, ...]
    broker_uid: int
    runtime_root: Path
    runtime_sha256: str
    staging_root: Path
    parent_slice: str
    input_limit: int = MAX_INPUT
    work_bytes: int = 16 * 1024 * 1024
    work_inodes: int = 128

    def validate(self):
        if (not _limits(self.limits) or not 0 < len(self.slots) <= 64
                or len(self.slots) != len(self.native_uids) or len(set(self.slots)) != len(self.slots)
                or any(re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', s) is None for s in self.slots)
                or len(set(self.native_uids)) != len(self.native_uids)
                or not self.client_uids or self.broker_uid <= 0
                or any(type(uid) is not int or uid <= 0 for uid in self.native_uids + self.client_uids)
                or set(self.native_uids) & (set(self.client_uids) | {self.broker_uid})
                or self.broker_uid in self.client_uids
                or re.fullmatch('[0-9a-f]{64}', self.runtime_sha256) is None
                or re.fullmatch(r'[A-Za-z0-9_-]+\.slice', self.parent_slice) is None
                or not 0 < self.input_limit <= MAX_INPUT or not 0 < self.work_bytes <= self.limits.memory_bytes
                or not 0 < self.work_inodes <= 65536):
            raise DomainUnavailable()
        for path in (self.runtime_root, self.staging_root):
            if (not path.is_absolute() or path != path.resolve() or
                    any(c in str(path) for c in ('\x00', ':', '\n', ' '))):
                raise DomainUnavailable()
        # Pins include source-fixed schema/profile version; identity never grants authority.
        fields = asdict(self)
        fields['identity'].pop('policy_sha256')
        fields['schema'] = 'metadata-v1'
        fields['profile'] = 'system-manager-monitor-v1'
        encoded = json.dumps(fields, default=str, sort_keys=True, separators=(',', ':')).encode()
        if hashlib.sha256(encoded).hexdigest() != self.identity.policy_sha256:
            raise DomainUnavailable()


def protected_directory(path, uid, *, private=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise DomainUnavailable()
    for component in (path, *path.parents):
        info = component.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022):
            raise DomainUnavailable()
    if private and path.stat().st_mode & 0o077:
        raise DomainUnavailable()


def runtime_digest(root, deadline):
    """Read immutable operator runtime without importing any installed module."""
    protected_directory(root, 0)
    digest = hashlib.sha256()
    count, total = 0, 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            if time.monotonic() >= deadline:
                raise DomainUnavailable()
            path = Path(parent) / name
            info = path.lstat()
            count += 1
            if (count > 100000 or info.st_uid != 0 or info.st_mode & 0o022
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise DomainUnavailable()
            digest.update((str(path.relative_to(root)) + ':' + str(info.st_mode) + '\n').encode())
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > 32 * 1024**3:
                    raise DomainUnavailable()
                with path.open('rb') as stream:
                    while data := stream.read(65536):
                        if time.monotonic() >= deadline:
                            raise DomainUnavailable()
                        digest.update(data)
    return digest.hexdigest()

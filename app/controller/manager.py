"""Restricted system-manager bridge; must run as an operator-owned service.

This source never installs a service or obtains privilege. It only uses the
system manager when independently provisioned root authority and pins verify.
No user-manager, shell, caller properties or privileged container fallback.
"""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import time

from app.core.native_domain import DomainUnavailable, Lease, domain_identity
from app.controller.policy import Policy, protected_directory, runtime_digest
from app.controller.monitor import capture


def runtime_usec(value):
    """Parse systemctl's finite duration readback, never accept infinity/max."""
    if type(value) is not str:
        raise DomainUnavailable()
    # systemctl show renders microsecond duration properties in human units.
    parts=re.findall(r'(\d+(?:\.\d+)?)(min|ms|us|s)',value)
    if not parts or ' '.join(number+unit for number,unit in parts) != value:
        raise DomainUnavailable()
    from decimal import Decimal
    factors={'min':60000000,'s':1000000,'ms':1000,'us':1}
    result=sum(Decimal(number)*factors[unit] for number,unit in parts)
    if result != int(result) or not 0 < result <= 305000000:
        raise DomainUnavailable()
    return int(result)


class KernelObserver:
    """Synthetic roots are useful in tests; real bridge fixes both to the host."""
    def __init__(self, cgroup_root=Path('/sys/fs/cgroup'), proc_root=Path('/proc')):
        self.root, self.proc = Path(cgroup_root), Path(proc_root)

    def path(self, group):
        if (type(group) is not str or not group.startswith('/')
                or any(part in ('.', '..') for part in group.split('/'))):
            raise DomainUnavailable()
        path = self.root / group.lstrip('/')
        try:
            if path.resolve() != path or not path.is_relative_to(self.root) or not path.is_dir():
                raise DomainUnavailable()
        except OSError as exc:
            raise DomainUnavailable() from exc
        return path

    def controls(self, group, limits):
        path = self.path(group)
        expected = {'memory.max': str(limits.memory_bytes), 'memory.swap.max': str(limits.swap_bytes),
                    'pids.max': str(limits.tasks), 'cpu.max': f'{limits.cpu_quota} {limits.cpu_period}'}
        try:
            if any((path / name).read_text().strip() != value for name, value in expected.items()):
                raise DomainUnavailable()
        except (OSError, ValueError) as exc:
            raise DomainUnavailable() from exc
        return path

    def member(self, group, pid, limits):
        try:
            path = self.controls(group, limits)
            # Before grant only the root monitor may inhabit this new domain.
            if (path / 'cgroup.procs').read_text().split() != [str(pid)]:
                raise DomainUnavailable()
            proc = self.proc / str(pid)
            if (proc / 'cgroup').read_text().strip() != '0::' + group:
                raise DomainUnavailable()
            status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
            if status['Uid'].split() != ['0', '0', '0', '0']:
                raise DomainUnavailable()
            ticks = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19])
            return {'cgroup': group, 'pid': pid, 'start_ticks': ticks,
                    'inode': path.stat().st_ino, 'device': path.stat().st_dev}
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise DomainUnavailable() from exc

    def drained(self, group, inode, device):
        try:
            path = self.path(group)
            info = path.stat()
            events = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
            # Refuse retained charges; do not reset counters or accept a timer.
            return (info.st_ino == inode and info.st_dev == device
                    and events.get('populated') == '0' and not (path / 'cgroup.procs').read_text().split()
                    and int((path / 'pids.current').read_text()) == 0
                    and int((path / 'memory.current').read_text()) == 0)
        except (OSError, DomainUnavailable, ValueError):
            # Removed cgroup needs separate proven manager teardown; handled there.
            return False


class SystemManager:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.observer = KernelObserver()
        self.records = {}
        self.runtime_verified = False

    @staticmethod
    def _command(command, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DomainUnavailable()
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'},
                                     close_fds=True)
            code, output, _ = capture(child, time.monotonic() + min(remaining, 5), 65536)
            if code or len(output.encode()) > 65536:
                raise DomainUnavailable()
            return output
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            raise DomainUnavailable() from exc

    def preflight(self):
        if os.geteuid() != 0 or self.policy is None:
            raise DomainUnavailable()
        p = self.policy
        p.validate()
        protected_directory(p.staging_root, 0, private=True)
        # The fixed runtime is a self-contained root image; nothing mounts the
        # Worker's application/secrets, control sockets or host runtime tree.
        if not self.runtime_verified:
            if runtime_digest(p.runtime_root, time.monotonic() + 5) != p.runtime_sha256:
                raise DomainUnavailable()
            for destination in ('usr/bin/python3', 'control', 'work', 'input.pdf', 'native-input'):
                if not (p.runtime_root / destination).exists():
                    raise DomainUnavailable()
            self.runtime_verified = True
        for uid in p.native_uids:
            entry = pwd.getpwuid(uid)
            if entry.pw_gid <= 0 or entry.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin', '/bin/false'):
                raise DomainUnavailable()
        deadline = time.monotonic() + 5
        parent = self._show(p.parent_slice, deadline)
        group = self.observer.path(parent['ControlGroup'])
        for file in ('memory.max', 'pids.max', 'cpu.max'):
            if (group / file).read_text().strip().split()[0] == 'max':
                raise DomainUnavailable()
        if (group / 'memory.swap.max').read_text().strip() == 'max':
            raise DomainUnavailable()
        # An operator must already have provided an independent system slice.
        if parent.get('ActiveState') != 'active' or not Path('/run/systemd/system').is_dir():
            raise DomainUnavailable()

    def inventory(self):
        output = self._command(['/usr/bin/systemctl', '--system', 'list-units', '--all', '--plain',
                                '--no-legend', 'native-*.service'], time.monotonic() + 5)
        units = [line.split()[0] for line in output.splitlines() if line.strip()]
        # A replaced/lost broker journal cannot hide private snapshots or
        # retained manager objects. Any leftover staging requires operator
        # reconciliation, including when its transient unit has disappeared.
        with os.scandir(self.policy.staging_root) as entries:
            for index, entry in enumerate(entries):
                if index >= 4096:
                    raise DomainUnavailable()
                units.append('retained-staging')
        return units

    def _lease(self, lease):
        p = self.policy
        if (type(lease) is not Lease or lease.identity != p.identity or lease.slot not in p.slots
                or type(lease.generation) is not int or not 0 < lease.generation < 2**63
                or re.fullmatch('[0-9a-f]{32}', lease.operation) is None
                or lease.domain != domain_identity(p.identity, lease.operation, lease.slot, lease.generation)):
            raise DomainUnavailable()
        return lease.domain + '.service'

    def _show(self, unit, deadline):
        result = self._command(['/usr/bin/systemctl', '--system', 'show', unit,
                                '--property=InvocationID,MainPID,ControlGroup,ActiveState,SubState,Result,RootDirectory,PrivateNetwork,PrivatePIDs,NoNewPrivileges,KillMode,RuntimeMaxUSec'], deadline)
        return dict(line.split('=', 1) for line in result.splitlines() if '=' in line)

    def launch_command(self, lease, directory):
        p = self.policy
        unit = self._lease(lease)
        properties = (
            f'Slice={p.parent_slice}', 'Type=exec', 'User=0', 'Group=0',
            'RemainAfterExit=yes', 'KillMode=control-group', 'SendSIGKILL=yes',
            f'RuntimeMaxSec={p.limits.wall_seconds + 5:g}', 'TimeoutStartSec=5', 'TimeoutStopSec=2',
            f'MemoryMax={p.limits.memory_bytes}', f'MemorySwapMax={p.limits.swap_bytes}',
            f'TasksMax={p.limits.tasks}', f'CPUQuota={100 * p.limits.cpu_quota / p.limits.cpu_period:g}%',
            f'CPUQuotaPeriodSec={p.limits.cpu_period}us', 'MemoryAccounting=yes', 'TasksAccounting=yes',
            'CPUAccounting=yes', f'RootDirectory={p.runtime_root}', 'ProtectSystem=strict',
            'ProtectHome=yes', 'PrivateNetwork=yes', 'PrivatePIDs=yes', 'PrivateIPC=yes',
            'PrivateDevices=yes', 'NoNewPrivileges=yes', 'RestrictSUIDSGID=yes',
            'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectControlGroups=yes',
            'ProtectKernelLogs=yes', 'ProtectClock=yes', 'RestrictRealtime=yes', 'LockPersonality=yes',
            'RestrictNamespaces=yes', 'RestrictAddressFamilies=AF_UNIX', 'ProtectProc=invisible',
            'ProcSubset=pid', 'CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_KILL',
            'SystemCallFilter=~@mount @reboot @swap @module @raw-io @debug',
            'InaccessiblePaths=/dev/shm /tmp /var/tmp /run /sys', 'ReadOnlyPaths=/dev',
            f'BindPaths={directory}:/control', f'BindReadOnlyPaths={directory / "input.pdf"}:/input.pdf',
            f'BindReadOnlyPaths={directory / "native-input"}:/native-input',
            f'TemporaryFileSystem=/work:rw,nosuid,nodev,noexec,size={p.work_bytes},nr_inodes={p.work_inodes},mode=1777',
            'ReadWritePaths=/control /work', 'WorkingDirectory=/work', 'StandardOutput=null', 'StandardError=null', 'UMask=0077',
            'Environment=LANG=C.UTF-8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 TMPDIR=/work',
        )
        return ['/usr/bin/systemd-run', '--system', '--quiet', '--unit=' + unit,
                *['--property=' + value for value in properties], '--',
                '/usr/bin/python3', '-I', '-m', 'app.controller.monitor', '/control/spec.json']

    def prepare(self, lease, manifest, deadline):
        self.preflight()
        self._lease(lease)
        if lease.domain in self.records:
            raise DomainUnavailable()
        from app.controller.profiles import validate_manifest, VERSION
        validate_manifest(manifest)
        input_bytes = len(manifest['input']) if manifest['kind'] == 'render_metadata' else len(manifest['request']) + sum(map(len,manifest['files'].values()))
        if input_bytes > self.policy.input_limit:
            raise DomainUnavailable()
        directory = self.policy.staging_root / lease.domain
        directory.mkdir(mode=0o700)
        native = directory / 'native-input'
        native.mkdir(mode=0o755)
        token = secrets.token_hex(32)
        uid = self.policy.native_uids[self.policy.slots.index(lease.slot)]
        data = manifest.get('input', b'placeholder')
        spec = {'domain': lease.domain, 'token': token, 'uid': uid, 'gid': pwd.getpwuid(uid).pw_gid,
                'wall_seconds': min(300, self.policy.limits.wall_seconds),
                'output_limit': 1024 if manifest['kind'] == 'render_metadata' else 65536,
                'input_sha256': hashlib.sha256(data).hexdigest(), 'kind':manifest['kind'],
                'profile':VERSION}
        if manifest['kind'] == 'native_operation':
            self._write(native / 'request.json',manifest['request'],0o444)
            for name, content in manifest['files'].items():
                self._write(native / name, content, 0o444)
        self._write(directory / 'input.pdf', data, 0o444)
        self._write(directory / 'spec.json', json.dumps(spec).encode(), 0o600)
        self._command(self.launch_command(lease, directory), deadline)
        while time.monotonic() < deadline:
            state = self._show(lease.domain + '.service', deadline)
            pid = int(state.get('MainPID', '0'))
            if pid > 0 and state.get('ActiveState') == 'active':
                if (not re.fullmatch('[0-9a-f]{32}', state.get('InvocationID', ''))
                        or state.get('RootDirectory') != str(self.policy.runtime_root)
                        or runtime_usec(state.get('RuntimeMaxUSec')) != round((self.policy.limits.wall_seconds + 5) * 1000000)
                        or any(state.get(key) != value for key, value in (
                            ('PrivateNetwork', 'yes'), ('PrivatePIDs', 'yes'),
                            ('NoNewPrivileges', 'yes'), ('KillMode', 'control-group')))):
                    raise DomainUnavailable()
                member = self.observer.member(state['ControlGroup'], pid, self.policy.limits)
                member.update(invocation=state['InvocationID'], token=token, lease=lease, sequence=0, event_bytes=0)
                self.records[lease.domain] = member
                return {key: value for key, value in member.items() if key != 'lease'}
            time.sleep(.02)
        raise DomainUnavailable()

    @staticmethod
    def _write(path, data, mode):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def grant(self, lease, token, deadline):
        record = self.records[lease.domain]
        if record['token'] != token:
            raise DomainUnavailable()
        state = self._show(self._lease(lease), deadline)
        observed = self.observer.member(record['cgroup'], record['pid'], self.policy.limits)
        if (state.get('InvocationID') != record['invocation']
                or any(observed[key] != record[key] for key in ('pid', 'start_ticks', 'inode', 'device'))):
            raise DomainUnavailable()
        self._write(self.policy.staging_root / lease.domain / 'grant', token.encode(), 0o600)

    def poll(self, lease, deadline):
        self._lease(lease)
        directory = self.policy.staging_root / lease.domain
        path = directory / 'complete.json'
        event_path = directory / 'event.json'
        if event_path.exists():
            from app.controller.events import validate_event
            event = self._read_json(event_path, 1024 * 1024)
            record = self.records[lease.domain]
            validate_event(event,record['sequence']+1)
            return {'event':event}
        if not path.exists():
            return None
        return self._read_result(path)

    def ack(self, lease, sequence, deadline):
        self._lease(lease)
        from app.controller.events import validate_event
        record = self.records[lease.domain]
        directory = self.policy.staging_root / lease.domain
        event = self._read_json(directory / 'event.json', 1024*1024)
        validate_event(event,record['sequence']+1)
        if type(sequence) is not int or sequence != event['sequence']:
            raise DomainUnavailable()
        record['event_bytes'] += len(json.dumps(event).encode())
        if record['event_bytes'] > 16*1024*1024: raise DomainUnavailable()
        self._write(directory / ('ack-' + str(sequence)),str(sequence).encode(),0o600)
        (directory / 'event.json').unlink()
        record['sequence'] = sequence

    @staticmethod
    def _read_json(path, limit):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > limit:
                raise DomainUnavailable()
            return json.loads(stream.read(limit+1))

    @staticmethod
    def _read_result(path):
        from app.controller.transport import unpack
        from app.controller.profiles import validate_result
        result = unpack(SystemManager._read_json(path,24*1024*1024))
        validate_result(result)
        return result

    def drain(self, lease, deadline):
        unit = self._lease(lease)
        record = self.records.get(lease.domain)
        if record is None:
            raise DomainUnavailable()
        # Root monitor completion certifies waitpid(ECHILD) after adopted-child
        # termination. Inactive alone never supplies the reaping assertion.
        result = self.poll(lease, deadline)
        self._command(['/usr/bin/systemctl', '--system', 'stop', unit], deadline)
        state = self._show(unit, deadline)
        same = state.get('InvocationID') == record['invocation']
        try:
            group = self.observer.path(record['cgroup'])
        except DomainUnavailable:
            group = None
        empty = (group is None and state.get('ActiveState') == 'inactive' and state.get('MainPID') == '0')
        if group is not None:
            empty = self.observer.drained(record['cgroup'], record['inode'], record['device'])
        if not same or not empty or result is None or result['reaped'] is not True:
            raise DomainUnavailable()
        return result

    def cleanup(self, lease, deadline):
        # Called only after independently verified drain + Worker digest ack.
        self._lease(lease)
        directory = self.policy.staging_root / lease.domain
        if directory.parent != self.policy.staging_root or directory.is_symlink():
            raise DomainUnavailable()
        shutil.rmtree(directory)
        if directory.exists():
            raise DomainUnavailable()
        self.records.pop(lease.domain, None)

    def abort(self, lease, deadline):
        # Stopping an exact canonical unit never asserts drainage/reusability.
        self._command(['/usr/bin/systemctl', '--system', 'stop', self._lease(lease)], deadline)

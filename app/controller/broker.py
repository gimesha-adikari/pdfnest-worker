"""Private durable finite lease authority. No native modules execute here."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from app.core.native_domain import (BusyReceipt, Capabilities, DomainUnavailable,
                                    Drain, Identity, Lease, Membership, domain_identity)


class Journal:
    """One live broker owns the private database; flock prevents split authority.

    History has a hard finite bound. Exhaustion refuses new work rather than
    discarding nonce/generation evidence. Offline operator reconciliation is
    required to rotate history. Recovery never manufactures FREE from a clock.
    """
    def __init__(self, path: Path, identity: Identity, slots: tuple[str, ...], history_limit=4096):
        import fcntl
        self.identity, self.slots = identity, slots
        self.history_limit = history_limit
        self.lock = threading.RLock()
        self.path = Path(path)
        if (not 0 < history_limit <= 65536 or not 0 < len(slots) <= 64
                or len(set(slots)) != len(slots)
                or any(re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', s) is None for s in slots)):
            raise DomainUnavailable()
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DomainUnavailable()
        # Protected directory makes sqlite's own pathname/sidecar opens safe.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            entry = os.fstat(fd)
            if (not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1
                    or entry.st_uid != os.geteuid() or entry.st_mode & 0o077):
                raise DomainUnavailable()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
        except (OSError, DomainUnavailable) as exc:
            if 'fd' in locals():
                os.close(fd)
            raise DomainUnavailable() from exc
        try:
            self.db = sqlite3.connect(self.path, timeout=1, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA max_page_count=32768')  # Finite <=128 MiB journal DB.
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                    operation TEXT, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nonces (operation TEXT PRIMARY KEY, slot TEXT NOT NULL,
                    generation INTEGER NOT NULL, domain TEXT NOT NULL, owner INTEGER NOT NULL,
                    state TEXT NOT NULL, digest TEXT, manager TEXT, expires REAL NOT NULL);
            ''')
            pin = json.dumps({'identity': asdict(identity), 'slots': slots, 'history': history_limit}, sort_keys=True)
            with self.transaction():
                old = self.db.execute("SELECT value FROM meta WHERE key='pin'").fetchone()
                if old is not None and old[0] != pin:
                    raise DomainUnavailable()
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('pin', ?)", (pin,))
                for slot in slots:
                    self.db.execute("INSERT OR IGNORE INTO slots VALUES (?,0,NULL,'FREE')", (slot,))
                active = self.db.execute("SELECT COUNT(*) FROM slots WHERE state!='FREE'").fetchone()[0]
                if active:
                    self.db.execute("UPDATE slots SET state='QUARANTINED' WHERE state!='FREE'")
                    self.db.execute("UPDATE nonces SET state='QUARANTINED' WHERE state!='FREE'")
                    self._block()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def close(self):
        if getattr(self, 'db', None) is not None:
            self.db.close()
            self.db = None
        if getattr(self, 'fd', None) is not None:
            os.close(self.fd)
            self.fd = None

    def _block(self):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('blocked','1')")

    def blocked(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM meta WHERE key='blocked'").fetchone() is not None

    def acquire(self, operation, owner, lifetime=300):
        if (type(operation) is not str or re.fullmatch('[0-9a-f]{32}', operation) is None
                or type(owner) is not int or owner <= 0 or not 0 < lifetime <= 305):
            raise DomainUnavailable()
        with self.transaction():
            if self.blocked():
                raise DomainUnavailable()
            old = self.db.execute('SELECT * FROM nonces WHERE operation=?', (operation,)).fetchone()
            if old:
                if old['owner'] != owner or old['state'] == 'FREE':
                    raise DomainUnavailable()
                return self._lease(old)
            if self.db.execute('SELECT COUNT(*) FROM nonces').fetchone()[0] >= self.history_limit:
                raise DomainUnavailable()
            row = self.db.execute("SELECT * FROM slots WHERE state='FREE' ORDER BY slot LIMIT 1").fetchone()
            if row is None:
                # An authentic atomic capacity response has zero durable writes.
                return BusyReceipt(self.identity, operation)
            generation = row['generation'] + 1
            if generation >= 2**63:
                raise DomainUnavailable()
            domain = domain_identity(self.identity, operation, row['slot'], generation)
            self.db.execute('INSERT INTO nonces VALUES (?,?,?,?,?,?,NULL,NULL,?)',
                            (operation, row['slot'], generation, domain, owner, 'LEASED', time.time() + lifetime))
            self.db.execute("UPDATE slots SET generation=?, operation=?, state='LEASED' WHERE slot=?",
                            (generation, operation, row['slot']))
            return Lease(self.identity, operation, row['slot'], generation, domain)

    def _lease(self, row):
        return Lease(self.identity, row['operation'], row['slot'], row['generation'], row['domain'])

    def _exact(self, lease):
        row = self.db.execute('SELECT * FROM nonces WHERE operation=?', (lease.operation,)).fetchone()
        if row is None or self._lease(row) != lease:
            raise DomainUnavailable()
        return row

    def status(self, lease):
        with self.lock:
            return dict(self._exact(lease))

    def transition(self, lease, expected, state, manager=None):
        allowed = {('LEASED', 'PREPARED'), ('PREPARED', 'RUNNING'),
                   ('RUNNING', 'DRAINING'), ('PREPARED', 'DRAINING'), ('LEASED', 'DRAINING')}
        if (expected, state) not in allowed:
            raise DomainUnavailable()
        with self.transaction():
            row = self._exact(lease)
            if self.blocked() or row['state'] != expected:
                raise DomainUnavailable()
            self.db.execute('UPDATE nonces SET state=?, manager=COALESCE(?,manager) WHERE operation=?',
                            (state, manager, lease.operation))
            self.db.execute('UPDATE slots SET state=? WHERE slot=?', (state, lease.slot))

    def drained(self, lease, digest):
        if type(digest) is not str or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise DomainUnavailable()
        with self.transaction():
            row = self._exact(lease)
            if self.blocked() or row['state'] != 'DRAINING':
                raise DomainUnavailable()
            self.db.execute("UPDATE nonces SET state='RELEASING',digest=? WHERE operation=?", (digest, lease.operation))
            self.db.execute("UPDATE slots SET state='RELEASING' WHERE slot=?", (lease.slot,))

    def release(self, lease, digest):
        with self.transaction():
            row = self._exact(lease)
            if self.blocked() or row['digest'] != digest or row['state'] not in ('RELEASING', 'FREE'):
                raise DomainUnavailable()
            if row['state'] == 'FREE':
                return  # Identical replay; never overwrites a newer slot owner.
            self.db.execute("UPDATE nonces SET state='FREE' WHERE operation=?", (lease.operation,))
            self.db.execute("UPDATE slots SET state='FREE',operation=NULL WHERE slot=? AND operation=?",
                            (lease.slot, lease.operation))

    def quarantine(self, operation):
        with self.transaction():
            self._block()  # Unknown nonce can represent a lost Acquire reply.
            row = self.db.execute('SELECT * FROM nonces WHERE operation=?', (operation,)).fetchone()
            if row is not None and row['state'] != 'FREE':
                self.db.execute("UPDATE nonces SET state='QUARANTINED' WHERE operation=?", (operation,))
                self.db.execute("UPDATE slots SET state='QUARANTINED' WHERE operation=?", (operation,))

    def snapshot(self):
        with self.lock:
            return {name: [dict(r) for r in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                    for name in ('meta', 'slots', 'nonces')}


class Broker:
    """Authenticated session handler; manager is a separate protected bridge.

    The manager object must implement real protected transport in deployment.
    Synthetic implementations exist only in tests and cannot establish trust.
    """
    def __init__(self, journal, manager, limits):
        self.journal, self.manager, self.limits = journal, manager, limits
        self.mutex = threading.RLock()
        self.sessions = {}
        self.prepared = {}
        self.results = {}
        self.manager.preflight()
        if self.manager.inventory():
            self.journal.quarantine('unknown-unit')
        if self.journal.blocked():
            raise DomainUnavailable()

    def capabilities(self):
        self.manager.preflight()
        if self.journal.blocked():
            raise DomainUnavailable()
        return Capabilities(self.journal.identity, self.journal.slots, self.limits,
                            True, True, True, True, True, True, True)

    def _own(self, lease, session):
        if self.sessions.get(lease.operation) != session or self.journal.blocked():
            raise DomainUnavailable()
        return self.journal.status(lease)

    def acquire(self, operation, owner, session, wait):
        import math
        if (type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 300
                or type(session) is not str or not 0 < len(session) <= 128):
            raise DomainUnavailable()
        deadline = time.monotonic() + wait
        while True:
            with self.mutex:
                previous = self.sessions.get(operation)
                if previous is not None and previous != session:
                    raise DomainUnavailable()
                lease = self.journal.acquire(operation, owner, min(self.limits.wall_seconds + 5, 305))
                if type(lease) is Lease:
                    self.sessions[operation] = session
                    return lease
            if time.monotonic() >= deadline:
                return lease
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def prepare(self, lease, session, manifest, deadline):
        with self.mutex:
            row = self._own(lease, session)
            if row['state'] != 'LEASED' or set(manifest) != {'kind', 'input'}:
                raise DomainUnavailable()
            if (manifest['kind'] != 'render_metadata' or type(manifest['input']) is not bytes
                    or not 0 < len(manifest['input']) <= 16 * 1024 * 1024):
                raise DomainUnavailable()
        try:
            receipt = self.manager.prepare(lease, manifest, deadline)
            member = Membership(lease, self.limits, receipt['token'], False)
            manager_identity = json.dumps({key: value for key, value in receipt.items() if key != 'token'}, sort_keys=True)
            self.journal.transition(lease, 'LEASED', 'PREPARED', manager_identity)
            with self.mutex:
                self.prepared[lease.operation] = member
            return member
        except BaseException:
            self.quarantine(lease.operation, session)
            raise

    def run(self, lease, session, token, deadline):
        with self.mutex:
            self._own(lease, session)
            member = self.prepared.get(lease.operation)
            if member is None or member.token != token:
                raise DomainUnavailable()
            # Durable one-use run intent precedes the only gate-opening call.
            self.journal.transition(lease, 'PREPARED', 'RUNNING')
        try:
            self.manager.grant(lease, token, deadline)
        except BaseException:
            self.quarantine(lease.operation, session)
            raise

    def poll(self, lease, session, deadline):
        self._own(lease, session)
        if self.journal.status(lease)['state'] != 'RUNNING':
            raise DomainUnavailable()
        return self.manager.poll(lease, deadline)

    def drain(self, lease, session, deadline):
        self._own(lease, session)
        row = self.journal.status(lease)
        if row['state'] == 'RELEASING':
            result, digest = self.results[lease.operation]
            return Drain(lease, True, True, True), result, digest
        self.journal.transition(lease, row['state'], 'DRAINING')
        try:
            result = self.manager.drain(lease, deadline)
            if (type(result) is not dict or set(result) != {'returncode', 'stdout', 'stderr', 'reaped'}
                    or result['reaped'] is not True or type(result['returncode']) is not int
                    or any(type(result[k]) is not str or len(result[k].encode()) > 1024 for k in ('stdout', 'stderr'))):
                raise DomainUnavailable()
            import hashlib
            digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.journal.drained(lease, digest)
            self.results[lease.operation] = result, digest
            return Drain(lease, True, True, True), result, digest
        except BaseException:
            self.quarantine(lease.operation, session)
            raise

    def release(self, lease, session, digest, deadline):
        self._own(lease, session)
        row = self.journal.status(lease)
        if row['state'] == 'FREE' and row['digest'] == digest:
            return
        if row['state'] != 'RELEASING' or row['digest'] != digest:
            raise DomainUnavailable()
        try:
            # The authenticated caller supplies the digest only after semantic
            # validation and removal of its own request staging. Controller
            # cleanup is independent and must finish before the FREE commit.
            self.manager.cleanup(lease, deadline)
            self.journal.release(lease, digest)
            self.prepared.pop(lease.operation, None)
            self.results.pop(lease.operation, None)
            # Keep bounded session binding for identical lost-release replay.
        except BaseException:
            self.quarantine(lease.operation, session)
            raise

    def quarantine(self, operation, session):
        owner = self.sessions.get(operation)
        if owner is not None and owner != session:
            raise DomainUnavailable()
        self.journal.quarantine(operation)
        rows = [row for row in self.journal.snapshot()['nonces'] if row['operation'] == operation]
        if rows and rows[0]['state'] != 'FREE':
            lease = self.journal._lease(rows[0])
            try:
                self.manager.drain(lease, time.monotonic() + 5)
            except BaseException:
                try:
                    self.manager.abort(lease, time.monotonic() + 5)
                except BaseException:
                    pass  # Durable block remains; independent unit deadline survives.

    def disconnect(self, session):
        for operation, owner in list(self.sessions.items()):
            if owner == session:
                rows = [r for r in self.journal.snapshot()['nonces'] if r['operation'] == operation]
                if rows and rows[0]['state'] != 'FREE':
                    self.quarantine(operation, session)

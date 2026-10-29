"""Synthetic manager tests: durable authority semantics, NOT kernel qualification."""
from dataclasses import replace
import time
import pytest
from app.core.native_domain import Identity, Limits, BusyReceipt, DomainUnavailable


def api():
    import importlib.util
    assert importlib.util.find_spec('app.controller') is not None, 'controller source is missing'
    from app.controller.broker import Journal, Broker
    return Journal, Broker


def identity():
    return Identity('local-controller', 'epoch-1', 'a' * 64)


def test_atomic_busy_does_not_change_nonce_or_generation(tmp_path):
    Journal, _ = api()
    journal = Journal(tmp_path / 'state.sqlite', identity(), ('one',), 4)
    lease = journal.acquire('a' * 32, 1000)
    before = journal.snapshot()
    assert journal.acquire('b' * 32, 1000) == BusyReceipt(identity(), 'b' * 32)
    assert journal.snapshot() == before
    assert journal.acquire('a' * 32, 1000) == lease


def test_restart_quarantines_live_lease_and_refuses_new_admission(tmp_path):
    Journal, _ = api()
    path = tmp_path / 'state.sqlite'
    journal = Journal(path, identity(), ('one',), 4)
    lease = journal.acquire('a' * 32, 1000)
    journal.close()
    recovered = Journal(path, identity(), ('one',), 4)
    assert recovered.status(lease)['state'] == 'QUARANTINED'
    with pytest.raises(DomainUnavailable):
        recovered.acquire('b' * 32, 1000)


def test_release_requires_exact_lease_drain_output_digest_and_cleanup_ack(tmp_path):
    Journal, _ = api()
    journal = Journal(tmp_path / 'state.sqlite', identity(), ('one',), 4)
    lease = journal.acquire('a' * 32, 1000)
    with pytest.raises(DomainUnavailable):
        journal.release(lease, 'd' * 64)
    journal.transition(lease, 'LEASED', 'PREPARED')
    journal.transition(lease, 'PREPARED', 'RUNNING')
    journal.transition(lease, 'RUNNING', 'DRAINING')
    journal.drained(lease, 'd' * 64)
    with pytest.raises(DomainUnavailable):
        journal.release(replace(lease, generation=lease.generation + 1), 'd' * 64)
    with pytest.raises(DomainUnavailable):
        journal.release(lease, 'e' * 64)
    journal.release(lease, 'd' * 64)
    assert journal.acquire('b' * 32, 1000).generation == 2
    with pytest.raises(DomainUnavailable):
        journal.transition(lease, 'LEASED', 'PREPARED')


def test_finite_nonce_history_refuses_instead_of_evicting_uncertainty(tmp_path):
    Journal, _ = api()
    journal = Journal(tmp_path / 'state.sqlite', identity(), ('one',), 1)
    lease = journal.acquire('a' * 32, 1000)
    journal.quarantine(lease.operation)
    with pytest.raises(DomainUnavailable):
        journal.acquire('b' * 32, 1000)
    assert len(journal.snapshot()['nonces']) == 1


def test_policy_drift_and_symlink_journal_refused(tmp_path):
    Journal, _ = api()
    path = tmp_path / 'state.sqlite'
    Journal(path, identity(), ('one',), 4).close()
    with pytest.raises(DomainUnavailable):
        Journal(path, replace(identity(), epoch='epoch-2'), ('one',), 4)
    link = tmp_path / 'linked.sqlite'
    link.symlink_to(path)
    with pytest.raises(DomainUnavailable):
        Journal(link, identity(), ('one',), 4)

class SyntheticManager:
    """No kernel or authority assertions; deterministic broker dependency."""
    def __init__(self):
        self.calls = []
        self.fail_drain = False
    def preflight(self):
        self.calls.append('preflight')
    def inventory(self):
        return []
    def prepare(self, lease, manifest, deadline):
        self.calls.append('prepare')
        assert manifest['kind'] == 'render_metadata'
        return {'token': 'gate-token', 'invocation': 'f' * 32, 'cgroup': '/synthetic'}
    def grant(self, lease, token, deadline):
        self.calls.append('grant')
    def poll(self, lease, deadline):
        return {'returncode': 0, 'stdout': '{"page_count": 2}', 'stderr': '', 'reaped': True}
    def drain(self, lease, deadline):
        self.calls.append('drain')
        if self.fail_drain:
            raise DomainUnavailable()
        return self.poll(lease, deadline)
    def cleanup(self, lease, deadline):
        self.calls.append('cleanup')


def make_broker(tmp_path):
    Journal, Broker = api()
    manager = SyntheticManager()
    limits = Limits(64 << 20, 0, 16, 50000, 100000, 30)
    return Broker(Journal(tmp_path / 'state.sqlite', identity(), ('one',), 4), manager, limits), manager


def test_broker_gate_and_release_require_owner_digest_and_cleanup(tmp_path):
    broker, manager = make_broker(tmp_path)
    assert hasattr(broker, 'acquire'), 'broker protocol is not implemented'
    deadline = time.monotonic() + 5
    lease = broker.acquire('a' * 32, 1000, 'session-a', 0)
    with pytest.raises(DomainUnavailable):
        broker.prepare(lease, 'session-b', {'kind': 'render_metadata', 'input': b'pdf'}, deadline)
    member = broker.prepare(lease, 'session-a', {'kind': 'render_metadata', 'input': b'pdf'}, deadline)
    assert 'grant' not in manager.calls
    broker.run(lease, 'session-a', member.token, deadline)
    assert manager.calls[-1] == 'grant'
    receipt, result, digest = broker.drain(lease, 'session-a', deadline)
    assert receipt.empty and receipt.reaped and receipt.reconciled
    with pytest.raises(DomainUnavailable):
        broker.release(lease, 'session-a', '0' * 64, deadline)
    assert 'cleanup' not in manager.calls
    broker.release(lease, 'session-a', digest, deadline)
    assert manager.calls[-1] == 'cleanup'
    assert broker.acquire('b' * 32, 1000, 'session-b', 0).generation == 2


def test_owner_disconnect_drains_but_quarantines_all_shared_admission(tmp_path):
    broker, manager = make_broker(tmp_path)
    assert hasattr(broker, 'acquire'), 'broker protocol is not implemented'
    lease = broker.acquire('a' * 32, 1000, 'session-a', 0)
    broker.prepare(lease, 'session-a', {'kind': 'render_metadata', 'input': b'pdf'}, time.monotonic() + 5)
    broker.disconnect('session-a')
    assert broker.journal.status(lease)['state'] == 'QUARANTINED'
    assert 'drain' in manager.calls
    with pytest.raises(DomainUnavailable):
        broker.capabilities()

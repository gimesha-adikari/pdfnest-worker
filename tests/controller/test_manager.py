"""Synthetic manager files exercise read-back logic, not actual kernel limits."""
from pathlib import Path
import os
import pytest
from app.core.native_domain import DomainUnavailable, Identity, Limits, Lease, domain_identity


def module():
    import importlib.util
    assert importlib.util.find_spec('app.controller.manager') is not None, 'system manager implementation is missing'
    from app.controller import manager
    return manager


def limits():
    return Limits(64 << 20, 0, 16, 50000, 100000, 30)


def test_real_preflight_refuses_unprivileged_authority_without_launching(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    with pytest.raises(DomainUnavailable):
        m.SystemManager(None).preflight()


def fixture_cgroup(tmp_path):
    cg = tmp_path / 'cgroup' / 'native-test.service'
    cg.mkdir(parents=True)
    for name, value in {'memory.max': str(64 << 20), 'memory.swap.max': '0',
                        'pids.max': '16', 'cpu.max': '50000 100000',
                        'cgroup.procs': '123\n', 'cgroup.events': 'populated 1\nfrozen 0\n',
                        'memory.current': '1000', 'pids.current': '1'}.items():
        (cg / name).write_text(value)
    proc = tmp_path / 'proc' / '123'
    proc.mkdir(parents=True)
    (proc / 'cgroup').write_text('0::/native-test.service\n')
    (proc / 'status').write_text('Uid:\t0\t0\t0\t0\n')
    (proc / 'stat').write_text('123 (monitor) S ' + '0 ' * 18 + '555 0 0')
    return cg, proc


def test_gate_reads_actual_aggregate_controls_and_membership(tmp_path):
    m = module()
    cg, proc = fixture_cgroup(tmp_path)
    observer = m.KernelObserver(cg.parent, proc.parent)
    receipt = observer.member('/native-test.service', 123, limits())
    assert receipt['start_ticks'] == 555
    (cg / 'memory.max').write_text('max')
    with pytest.raises(DomainUnavailable):
        observer.member('/native-test.service', 123, limits())


@pytest.mark.parametrize('bad', ['memory.swap.max', 'pids.max', 'cpu.max', 'cgroup.procs'])
def test_gate_missing_controls_or_wrong_member_refused(tmp_path, bad):
    m = module()
    cg, proc = fixture_cgroup(tmp_path)
    (cg / bad).unlink()
    with pytest.raises(DomainUnavailable):
        m.KernelObserver(cg.parent, proc.parent).member('/native-test.service', 123, limits())


def test_command_properties_are_fixed_not_request_selected(tmp_path):
    m = module()
    from app.controller.policy import Policy
    p = Policy(Identity('controller', 'e1', 'a' * 64), limits(), ('s1',), (65001,), (1000,),
               65000, tmp_path / 'runtime', 'b' * 64, tmp_path / 'stage', 'native-controller.slice')
    lease = Lease(p.identity, 'a' * 32, 's1', 1,
                  domain_identity(p.identity, 'a' * 32, 's1', 1))
    command = m.SystemManager(p).launch_command(lease, tmp_path / 'stage' / lease.domain)
    joined = ' '.join(command)
    assert command[:2] == ['/usr/bin/systemd-run', '--system']
    for property in ('KillMode=control-group', 'PrivateNetwork=yes', 'PrivatePIDs=yes',
                     'NoNewPrivileges=yes', 'MemoryMax=67108864', 'MemorySwapMax=0',
                     'TasksMax=16', 'RuntimeMaxSec=35', 'User=0'):
        assert property in joined
    assert command[-4:] == ['-I', '-m', 'app.controller.monitor', '/control/spec.json']
    assert 'PYTHONPATH' not in joined


def test_unaccounted_staging_is_inventory_uncertainty_not_a_clean_restart(tmp_path, monkeypatch):
    m = module()
    from app.controller.policy import Policy
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    (stage / ('native-' + 'c' * 64)).mkdir(mode=0o700)
    policy = Policy(Identity('c', 'e', 'a' * 64), limits(), ('s',), (65001,), (1000,),
                    65000, tmp_path / 'runtime', 'b' * 64, stage, 'native-controller.slice')
    manager = m.SystemManager(policy)
    monkeypatch.setattr(manager, '_command', lambda *args: '')
    assert manager.inventory(), 'retained staging must prevent a fresh-journal readiness claim'


@pytest.mark.parametrize('value,expected',[('35s',35000000),('5min 5s',305000000),('1.5s',1500000),('35000000us',35000000)])
def test_independent_runtime_deadline_readback_is_finite_and_exact(value,expected):
    assert module().runtime_usec(value)==expected


@pytest.mark.parametrize('value',['infinity','max','0','306s','5min5s','NaNs',None])
def test_missing_or_unbounded_independent_deadline_refused(value):
    with pytest.raises(DomainUnavailable):module().runtime_usec(value)

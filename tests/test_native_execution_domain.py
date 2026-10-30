"""Source-contract tests; the fake controller is NOT kernel enforcement."""
from dataclasses import replace
import subprocess
from queue import Queue
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Optional
import pytest

from app.core import native_domain as d


class FakeController:
    """Deterministic trusted test double. Never registered by application code."""
    identity = d.Identity('synthetic-controller', 'epoch-1', 'a' * 64)

    def __init__(self):
        self.events = []
        self.generation = 0
        self.busy = False
        self.dirty = False
        self.fail: Optional[BaseException] = None
        self.limits = d.Limits(256 << 20, 0, 16, 50000, 100000, 300.0)

    def capabilities(self, deadline):
        return d.Capabilities(self.identity, ('slot-1',), self.limits,
                              independent=True, gated=True, protected=True,
                              recovery_verified=not self.dirty, bounded=True, domain_kill=True, accounted=True)

    def acquire(self, operation, deadline, check):
        check()
        if self.busy:
            return d.BusyReceipt(self.identity, operation)
        self.busy = True
        self.generation += 1
        self.events.append('LEASED')
        return d.Lease(self.identity, operation, 'slot-1', self.generation,
                       d.domain_identity(self.identity, operation, 'slot-1', self.generation))

    def prepare(self, lease, launch, deadline):
        self.events.append('PREPARED')
        return d.Membership(lease, self.limits, 'gated-member', False)

    def run(self, lease, member, launch, deadline, check):
        self.events.append('RUNNING')
        check()
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(launch.command, 0, 'ok', '')

    def drain(self, lease, deadline):
        self.events.append('DRAINING')
        return d.Drain(lease, not self.dirty, not self.dirty, not self.dirty)

    def release(self, lease, deadline):
        self.events.append('FREE')
        self.busy = False

    def quarantine(self, lease, deadline):
        self.events.append('QUARANTINED')
        self.dirty = True


@pytest.fixture
def controller(monkeypatch):
    c = FakeController()
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', d.Registration(c, c.identity, c.limits, ('slot-1',)))
    return c


def admission():
    from contextlib import nullcontext
    return d.admit(lambda: None, 0, lambda: nullcontext())


def run(domain, check=lambda: None):
    return domain.run(['synthetic'], timeout=1, cancellation_check=check,
                      env={}, max_output_bytes=1024)


def test_admission_gate_drain_cleanup_release_order(controller):
    with admission() as domain:
        assert run(domain).stdout == 'ok'
        domain.drain()
        controller.events.append('VALIDATED_AND_CLEANED')
        domain.cleanup_complete()
    assert controller.events == ['LEASED', 'PREPARED', 'RUNNING', 'DRAINING',
                                 'VALIDATED_AND_CLEANED', 'FREE']


def test_no_controller_never_starts_legacy(monkeypatch):
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', None)
    with pytest.raises(d.DomainUnavailable, match='Native execution domain is unavailable'):
        with admission():
            pytest.fail('Native work started')


@pytest.mark.parametrize('field,value', [('independent', False), ('gated', False),
    ('protected', False), ('recovery_verified', False), ('bounded', False),
    ('domain_kill', False), ('accounted', False)])
def test_missing_controller_capability_fails_before_admission(controller, field, value):
    original = controller.capabilities
    controller.capabilities = lambda deadline: replace(original(deadline), **{field: value})
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Unverified admission')
    assert not controller.events


@pytest.mark.parametrize('field,value', [('memory_bytes', 0), ('swap_bytes', -1),
    ('tasks', 0), ('cpu_quota', 0), ('cpu_period', 0), ('wall_seconds', float('inf'))])
def test_missing_finite_limits_rejected(controller, field, value):
    controller.limits = replace(controller.limits, **{field: value})
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Missing resource ceiling')
    assert not controller.events


def test_untrusted_controller_identity(controller):
    controller.identity = replace(controller.identity, epoch='untrusted')
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pass


def test_wrong_membership_never_runs(controller):
    original = controller.prepare
    controller.prepare = lambda *args: replace(original(*args), lease=replace(args[0], domain='wrong-domain'))
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            run(domain)
    assert 'RUNNING' not in controller.events
    assert controller.events[-1] == 'QUARANTINED'


def test_stale_generation_quarantines_without_execution(controller):
    with admission() as domain:
        run(domain);domain.drain();domain.cleanup_complete()
    controller.generation = 0
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Stale lease')
    assert controller.events.count('RUNNING') == 1
    assert controller.events[-1] == 'QUARANTINED'


def test_capacity_busy_does_not_fallback(controller):
    controller.busy = True
    with pytest.raises(d.DomainBusy):
        with admission():
            pytest.fail('Over-admitted')
    assert not controller.events


@pytest.mark.parametrize('failure', [RuntimeError('sensitive path /private/input'),
    subprocess.TimeoutExpired('synthetic', 1), KeyboardInterrupt()])
def test_interruption_drains_but_never_releases_unvalidated_operation(controller, failure):
    controller.fail = failure
    with pytest.raises(type(failure)):
        with admission() as domain:
            run(domain)
    assert controller.events[-2:] == ['DRAINING', 'QUARANTINED']
    assert controller.busy


def test_dirty_domain_cannot_succeed_or_reuse(controller):
    controller.dirty = True
    controller.capabilities = lambda deadline: d.Capabilities(
        controller.identity, ('slot-1',), controller.limits, True, True, True, True, True, True, True)
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert controller.events[-1] == 'QUARANTINED'
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Reused quarantined registration')


def test_next_operation_only_after_verified_cleanup(controller):
    for _ in range(2):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert controller.events.count('FREE') == 2


def test_unknown_mode_fails_closed(monkeypatch):
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'requiredd')
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pass


def test_native_launcher_refuses_missing_controller_before_legacy_work(monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    from app.core.ocr_v2.errors import EngineUnavailableError
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', None)
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(boundary, 'run_hardened_subprocess',
                        lambda *a, **kw: pytest.fail('Legacy child started'))
    with pytest.raises(EngineUnavailableError, match='Native execution domain is unavailable'):
        boundary.execute('inspect_pdf', 'unused-synthetic-input')
    assert not list(tmp_path.iterdir())


def test_metadata_launcher_refuses_missing_controller_before_legacy_work(monkeypatch, tmp_path):
    from app.api.tools.render import session_metadata as metadata
    from app.core import subprocess_runner
    import threading
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', None)
    monkeypatch.setattr(subprocess_runner, 'run_hardened_subprocess',
                        lambda *a, **kw: pytest.fail('Legacy metadata child started'))
    with pytest.raises(metadata.MetadataUnavailable):
        metadata._inspect(tmp_path / 'unused.pdf', threading.Event())


@pytest.fixture
def file_result(controller):
    from pathlib import Path
    from app.core import native_isolation as boundary
    def fake_run(lease, member, launch, deadline, check):
        controller.events.append('RUNNING')
        check()
        root = Path(launch.command[3])
        request = boundary.read_frame(root / 'request.json')
        args = boundary.decode(request['args'])
        Path(args[1]).write_bytes(b'SYNTHETIC OUTPUT')
        boundary.write_frame(root / 'result.json', {'result': boundary.encode({'done': True})})
        return subprocess.CompletedProcess(launch.command, 0, '', '')
    controller.run = fake_run
    return controller


def test_native_file_publication_follows_drain_validation_cleanup_release(file_result, monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    output = tmp_path / 'output.pdf'
    output.write_bytes(b'PREVIOUS RESULT')
    original = file_result.release
    def release(lease, deadline):
        assert output.read_bytes() == b'PREVIOUS RESULT'
        assert not list(tmp_path.glob('platen-native-*'))
        original(lease, deadline)
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    file_result.release = release
    result = boundary.execute('compile_document', 'synthetic.pdf', output, 'pages.json')
    assert result == {'done': True}
    assert output.read_bytes() == b'SYNTHETIC OUTPUT'
    assert not list(tmp_path.glob('.native-output-*'))
    assert file_result.events[-2:] == ['DRAINING', 'FREE']


def test_failed_drain_preserves_previous_output_and_quarantines_private_staging(file_result, monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    from app.core.ocr_v2.errors import EngineUnavailableError
    output = tmp_path / 'output.pdf'
    output.write_bytes(b'PREVIOUS RESULT')
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    original = file_result.run
    def run_dirty(*args):
        result = original(*args)
        file_result.dirty = True
        return result
    file_result.run = run_dirty
    with pytest.raises(EngineUnavailableError, match='Native execution domain is unavailable'):
        boundary.execute('compile_document', 'synthetic.pdf', output, 'pages.json')
    assert output.read_bytes() == b'PREVIOUS RESULT'
    assert len(list(tmp_path.glob('platen-native-*'))) == 1
    assert not list(tmp_path.glob('.native-output-*'))
    assert 'FREE' not in file_result.events
    assert file_result.events[-1] == 'QUARANTINED'
    # tmp_path cleanup is test-owned. Runtime quarantine requires an operator.


def test_callback_exception_preserved_after_drain_and_quarantine(controller):
    error = ValueError('synthetic callback failure')
    def callback():
        raise error
    with pytest.raises(ValueError) as caught:
        with admission() as domain:
            run(domain, callback)
    assert caught.value is error
    assert controller.events[-2:] == ['DRAINING', 'QUARANTINED']


def test_interrupted_controller_latches_refusal_even_when_quarantine_fails(controller):
    controller.run = lambda *a: (_ for _ in ()).throw(OSError('private controller socket'))
    controller.quarantine = lambda *a: (_ for _ in ()).throw(OSError('private path'))
    with pytest.raises(d.DomainUnavailable) as error:
        with admission() as domain:
            run(domain)
    assert str(error.value) == 'Native execution domain is unavailable'
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Reused uncertain controller')


@pytest.mark.parametrize('field,value', [('empty', False), ('reaped', False), ('reconciled', False)])
def test_incomplete_drain_receipt_cannot_release(controller, field, value):
    original = controller.drain
    controller.drain = lambda *a: replace(original(*a), **{field: value})
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert 'FREE' not in controller.events


def test_stale_drain_generation_is_not_accepted(controller):
    original = controller.drain
    controller.drain = lambda *a: replace(original(*a), lease=replace(a[0], generation=999))
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert 'FREE' not in controller.events


@pytest.mark.parametrize('mode', ['success', 'cancel', 'timeout'])
def test_known_detached_synthetic_child_stopped_by_test_double_before_release(controller, mode):
    """Real tiny child, FAKE controller tracking one known PID, no kernel claim."""
    import os
    import sys
    import tempfile
    child = None
    class Cancelled(Exception):
        pass
    def launch(lease, member, spec, deadline, check):
        nonlocal child
        script = ('import tempfile,os,sys,select;'
                  'f=tempfile.TemporaryFile();f.write(b"F11TINY\\n");f.flush();'
                  'print("READY",flush=True);select.select([sys.stdin],[],[],4)')
        child = subprocess.Popen([sys.executable, '-I', '-S', '-c', script],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True, env={'PATH': '/usr/bin:/bin', 'TMPDIR': tempfile.gettempdir()})
        assert child.stdout.readline() == b'READY\n'
        assert os.getpgid(child.pid) == child.pid
        controller.events.append('RUNNING')
        if mode == 'timeout':
            raise subprocess.TimeoutExpired('synthetic-controller', 1)
        check()
        return subprocess.CompletedProcess(spec.command, 0, 'ok', '')
    original = controller.drain
    def drain(lease, deadline):
        assert child is not None
        child.kill();child.wait(timeout=2)
        child.stdin.close();child.stdout.close()
        return original(lease, deadline)
    controller.run, controller.drain = launch, drain
    def check():
        if child is not None and mode == 'cancel':
            raise Cancelled()
    try:
        if mode == 'success':
            with admission() as domain:
                run(domain, check);domain.drain();domain.cleanup_complete()
            assert controller.events[-1] == 'FREE'
        else:
            with pytest.raises(Cancelled if mode == 'cancel' else subprocess.TimeoutExpired):
                with admission() as domain:
                    run(domain, check)
            assert controller.events[-1] == 'QUARANTINED'
        assert child.poll() is not None
    finally:
        if child is not None and child.poll() is None:
            child.kill();child.wait(timeout=2)
        if child is not None:
            child.stdin.close();child.stdout.close()


def test_owner_recovery_unverified_refuses_before_acquisition(controller):
    # Simulated owner/crash recovery state, not an actual independent watchdog.
    controller.dirty = True
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Unreconciled owner epoch')
    assert not controller.events


def test_repeated_async_cancel_joins_drain(controller, monkeypatch, tmp_path):
    import asyncio
    import threading
    from app.core import native_isolation as boundary
    ready, draining, release = threading.Event(), threading.Event(), threading.Event()
    def launch(lease, member, spec, deadline, check):
        ready.set()
        while not release.wait(.01):
            check()
        raise AssertionError('Cancellation not observed')
    def drain(lease, deadline):
        draining.set()
        assert release.wait(2)
        return d.Drain(lease, True, True, True)
    controller.run, controller.drain = launch, drain
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    async def scenario():
        task = asyncio.create_task(boundary.execute_async('inspect_pdf', 'synthetic.pdf'))
        assert await asyncio.to_thread(ready.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(draining.wait, 2)
        task.cancel()
        await asyncio.sleep(0)  # Deliver second cancellation at the drain barrier.
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(tmp_path.glob('platen-native-*'))
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_controller_cannot_overadmit_locally_active_slot(controller):
    with pytest.raises(d.DomainUnavailable):
        with admission() as first:
            controller.busy = False  # Deliberately faulty controller response.
            with pytest.raises(d.DomainUnavailable):
                with admission():
                    pytest.fail('Concurrent lease bypass')
            assert len(d._registration.active) == 1
            run(first)
    assert 'FREE' not in controller.events


def test_unreviewed_finite_policy_change_is_rejected(controller):
    controller.limits = replace(controller.limits, memory_bytes=512 << 20)
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Controller altered pinned policy')
    assert not controller.events


def test_early_admission_cancellation_is_preserved(controller):
    class Cancelled(Exception):
        pass
    # Callback identity belongs to caller, not an arbitrary controller exception.
    def cancel():
        raise Cancelled()
    with pytest.raises(Cancelled):
        with d.admit(cancel, 0, lambda: pytest.fail('Legacy capacity')):
            pass


def test_metadata_valid_count_and_failed_drain_are_distinct(controller, monkeypatch, tmp_path):
    from app.api.tools.render import session_metadata as metadata
    import threading
    controller.run = lambda lease, member, launch, deadline, check: subprocess.CompletedProcess(
        launch.command, 0, '{"page_count":2}', '')
    monkeypatch.setattr(metadata.tempfile, 'tempdir', str(tmp_path))
    assert metadata._inspect(tmp_path / 'synthetic.pdf', threading.Event()) == 2
    assert controller.events[-1] == 'FREE'
    def dirty(lease, member, launch, deadline, check):
        controller.dirty = True
        return subprocess.CompletedProcess(launch.command, 0, '{"page_count":2}', '')
    controller.run = dirty
    with pytest.raises(metadata.MetadataUnavailable):
        metadata._inspect(tmp_path / 'synthetic.pdf', threading.Event())
    assert controller.events[-1] == 'QUARANTINED'


def test_metadata_domain_busy_preserves_busy_error(controller, tmp_path):
    from app.api.tools.render import session_metadata as metadata
    import threading
    controller.busy = True
    with pytest.raises(metadata.MetadataBusy):
        metadata._inspect(tmp_path / 'synthetic.pdf', threading.Event())


def test_legacy_default_routes_to_original_runner_and_capacity(monkeypatch):
    from contextlib import contextmanager
    monkeypatch.delenv('NATIVE_EXECUTION_DOMAIN_MODE', raising=False)
    events = []
    @contextmanager
    def legacy_capacity():
        events.append('legacy-enter')
        yield
        events.append('legacy-exit')
    def runner(command, **kwargs):
        events.append(('legacy-run', command, kwargs['cleanup_group_on_exit']))
        return subprocess.CompletedProcess(command, 0, 'legacy', '')
    with d.admit(lambda: None, 0, legacy_capacity) as domain:
        assert not domain.required
        result = domain.run(['old'], timeout=1, env={}, max_output_bytes=8,
                            legacy_runner=runner, cleanup_group_on_exit=True)
        domain.drain();domain.cleanup_complete()
    assert result.stdout == 'legacy'
    assert events == ['legacy-enter', ('legacy-run', ['old'], True), 'legacy-exit']


def test_manager_and_domain_configuration_not_forwarded_to_child(controller):
    def launch(lease, member, spec, deadline, check):
        assert dict(spec.env) == {'TMPDIR': '/synthetic-staging'}
        return subprocess.CompletedProcess(spec.command, 0, '', '')
    controller.run = launch
    with admission() as domain:
        domain.run(['synthetic'], timeout=1, env={
            'TMPDIR': '/synthetic-staging', 'DBUS_SESSION_BUS_ADDRESS': 'synthetic-bus',
            'DBUS_SYSTEM_BUS_ADDRESS': 'synthetic-system', 'XDG_RUNTIME_DIR': '/synthetic-runtime',
            'NATIVE_EXECUTION_DOMAIN_MODE': 'required-nonproduction'})
        domain.drain();domain.cleanup_complete()


def test_native_environment_does_not_forward_service_credentials_or_loader_hooks(controller):
    captured = []
    def launch(lease, member, spec, deadline, check):
        captured.append(dict(spec.env))
        return subprocess.CompletedProcess(spec.command, 0, '', '')
    controller.run = launch
    with admission() as domain:
        domain.run(['synthetic'], timeout=1, env={
            'TMPDIR': '/synthetic-staging', 'OMP_NUM_THREADS': '1',
            'OCR_TEXT_ENGINE': 'sdk', 'TESSDATA_PREFIX': '/reviewed/tessdata',
            'AWS_SECRET_ACCESS_KEY': 'synthetic-secret', 'REDIS_URL': 'synthetic-secret',
            'WORKER_AUTH_SECRET': 'synthetic-secret', 'PYTHONPATH': '/mutable/runtime',
            'PYTHONSTARTUP': '/mutable/startup.py', 'LD_PRELOAD': '/mutable/loader.so',
            'OCR_UNREVIEWED_SECRET': 'synthetic-secret'})
        domain.drain();domain.cleanup_complete()
    assert captured == [{'TMPDIR': '/synthetic-staging', 'OMP_NUM_THREADS': '1',
                         'OCR_TEXT_ENGINE': 'sdk', 'TESSDATA_PREFIX': '/reviewed/tessdata'}]


@pytest.mark.parametrize('method', ['capabilities', 'acquire', 'prepare', 'run', 'drain'])
def test_late_controller_receipt_cannot_advance_or_release(controller, monkeypatch, method):
    # Deterministic clock, no timing sleeps and no claim of an independent timer.
    clock = [100.0]
    monkeypatch.setattr(d.time, 'monotonic', lambda: clock[0])
    original = getattr(controller, method)
    def late(*args):
        response = original(*args)
        clock[0] = args[-1] + 1 if method in ('capabilities', 'prepare', 'drain') else (
            args[-2] + 1 if method == 'run' else args[1] + d.CONTROL_SECONDS + 1)
        return response
    setattr(controller, method, late)
    with pytest.raises(subprocess.TimeoutExpired if method == 'run' else d.DomainUnavailable):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert 'FREE' not in controller.events
    assert d._registration.blocked
    if method in ('capabilities', 'acquire', 'prepare'):
        assert 'RUNNING' not in controller.events


@pytest.mark.parametrize('method', ['capabilities', 'acquire', 'prepare', 'run'])
def test_cancellation_after_controller_response_prevents_next_transition(controller, method):
    class Cancelled(Exception):
        pass
    cancelled = False
    original = getattr(controller, method)
    def cancel_after(*args):
        nonlocal cancelled
        response = original(*args)
        cancelled = True
        return response
    setattr(controller, method, cancel_after)
    def check():
        if cancelled:
            raise Cancelled()
    with pytest.raises(Cancelled):
        with d.admit(check, 0, lambda: pytest.fail('Legacy capacity')) as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    assert 'FREE' not in controller.events and d._registration.blocked
    if method in ('capabilities', 'acquire', 'prepare'):
        assert 'RUNNING' not in controller.events


@pytest.mark.parametrize('options', [
    {'timeout': True}, {'timeout': float('nan')}, {'timeout': '1'},
    {'max_output_bytes': 0}, {'max_output_bytes': True}, {'max_output_bytes': 65537},
    {'env': {'TMPDIR': '/bad\x00path'}}, {'command': []},
    {'command': ['bad\x00command']}, {'command': [object()]},
])
def test_invalid_launch_contract_rejected_before_preparation(controller, options):
    launch = dict(command=['synthetic'], timeout=1, max_output_bytes=1024, env={})
    launch.update(options)
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            domain.run(**launch)
    assert 'PREPARED' not in controller.events and 'RUNNING' not in controller.events
    assert 'FREE' not in controller.events and d._registration.blocked


def test_release_failure_never_publishes_staged_output(file_result, monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    from app.core.ocr_v2.errors import EngineUnavailableError
    output = tmp_path / 'output.pdf';output.write_bytes(b'OLD')
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    file_result.release = lambda *a: (_ for _ in ()).throw(OSError('synthetic private controller'))
    with pytest.raises(EngineUnavailableError, match='Native execution domain is unavailable'):
        boundary.execute('compile_document', 'synthetic.pdf', output, 'pages.json')
    assert output.read_bytes() == b'OLD'
    assert not list(tmp_path.glob('.native-output-*'))
    assert not list(tmp_path.glob('platen-native-*'))
    assert file_result.events[-1] == 'QUARANTINED'


def test_prepared_member_already_running_is_rejected(controller):
    original = controller.prepare
    controller.prepare = lambda *a: replace(original(*a), native_started=True)
    with pytest.raises(d.DomainUnavailable):
        with admission() as domain:
            run(domain)
    assert 'RUNNING' not in controller.events


def test_callback_cancel_during_drain_cannot_publish(file_result, monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    from app.jobs.cancellation import JobCancelledException
    import threading
    cancelled = threading.Event()
    output = tmp_path / 'output.pdf';output.write_bytes(b'OLD')
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    original = file_result.drain
    def drain(*args):
        receipt = original(*args)
        cancelled.set()
        return receipt
    def check():
        if cancelled.is_set():
            raise JobCancelledException('Synthetic cancellation')
    file_result.drain = drain
    with pytest.raises(JobCancelledException):
        boundary.execute('compile_document', 'synthetic.pdf', output, 'pages.json', cancellation_check=check)
    assert output.read_bytes() == b'OLD'
    assert not list(tmp_path.glob('.native-output-*'))
    assert 'FREE' not in file_result.events


def test_cleanup_failure_quarantines_without_release(file_result, monkeypatch, tmp_path):
    from app.core import native_isolation as boundary
    from app.core.ocr_v2.errors import EngineUnavailableError
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    original = d.shutil.rmtree
    def cleanup(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path / 'platen-native-')):
            raise OSError('synthetic staging cleanup failure')
        return original(path, *args, **kwargs)
    output = tmp_path / 'output.pdf';output.write_bytes(b'OLD')
    monkeypatch.setattr(d.shutil, 'rmtree', cleanup)
    with pytest.raises(EngineUnavailableError, match='Native execution domain is unavailable'):
        boundary.execute('compile_document', 'synthetic.pdf', output, 'pages.json')
    assert output.read_bytes() == b'OLD'
    assert 'FREE' not in file_result.events
    assert file_result.events[-1] == 'QUARANTINED'
    assert d._registration.blocked


def test_metadata_http_refusal_is_safe_and_creates_no_session(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.tools.render import router, service, session, session_metadata
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', None)
    root = tmp_path / 'sessions';root.mkdir()
    temporary = tmp_path / 'temporary';temporary.mkdir()
    monkeypatch.setattr(session, 'SESSION_ROOT', root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, 'session_manager', manager)
    monkeypatch.setattr(session_metadata.tempfile, 'tempdir', str(temporary))
    app = FastAPI();app.include_router(router.router)
    with TestClient(app) as client:
        response = client.post('/api/v1/render/sessions', files={
            'file': ('synthetic.pdf', b'%PDF-1.4\nsynthetic', 'application/pdf')})
    assert response.status_code == 503
    assert response.json() == {'detail': {
        'code': 'SESSION_METADATA_UNAVAILABLE',
        'message': 'PDF metadata processing is temporarily unavailable.'}}
    assert not manager._sessions and not list(root.iterdir())
    assert not list(temporary.iterdir())


def test_metadata_legacy_validation_remains_after_capacity_release(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import threading
    from app.core import native_isolation as boundary, subprocess_runner
    from app.api.tools.render import session_metadata as metadata
    monkeypatch.delenv('NATIVE_EXECUTION_DOMAIN_MODE', raising=False)
    held = False
    @contextmanager
    def capacity(*args):
        nonlocal held
        held = True
        yield
        held = False
    monkeypatch.setattr(boundary, '_capacity', capacity)
    monkeypatch.setattr(subprocess_runner, 'run_hardened_subprocess',
                        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, '{"page_count":1}', ''))
    original = metadata._verified_count
    def validate(*args):
        assert not held
        return original(*args)
    monkeypatch.setattr(metadata, '_verified_count', validate)
    assert metadata._inspect(tmp_path / 'synthetic.pdf', threading.Event()) == 1


def test_partial_acquire_busy_is_uncertain_and_quarantined(controller):
    original = controller.acquire
    operations = []
    def acquire(operation, deadline, check):
        original(operation, deadline, check)
        operations.append(operation)
        raise d.DomainBusy()
    controller.acquire = acquire
    with pytest.raises(d.DomainUnavailable) as caught:
        with admission():
            pytest.fail('Native work after indeterminate admission')
    assert type(caught.value) is d.DomainUnavailable
    assert d._registration.blocked
    assert controller.events == ['LEASED', 'QUARANTINED']
    assert len(operations) == 1


def test_same_slot_a_b_a_domain_reuse_is_rejected(controller):
    original = controller.acquire
    domains = []
    def acquire(*args):
        lease = original(*args)
        if len(domains) == 2:
            return replace(lease, domain=domains[0])
        domains.append(lease.domain)
        return lease
    controller.acquire = acquire
    for _ in range(2):
        with admission() as domain:
            run(domain);domain.drain();domain.cleanup_complete()
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Reused domain A after B')
    assert d._registration.blocked
    assert controller.events.count('RUNNING') == 2


def test_atomic_busy_receipt_changes_no_acquisition_state_and_allows_later_work(controller):
    # Full synthetic state snapshot, not an inference from an exception name.
    controller.busy = True
    original = controller.acquire
    observations = []
    def acquire(operation, deadline, check):
        before = (controller.busy, controller.generation, controller.dirty, tuple(controller.events))
        result = original(operation, deadline, check)
        after = (controller.busy, controller.generation, controller.dirty, tuple(controller.events))
        observations.append((operation, before, after, result))
        return result
    controller.acquire = acquire
    with pytest.raises(d.DomainBusy):
        with admission():
            pytest.fail('Busy refusal started work')
    operation, before, after, receipt = observations[0]
    assert before == after == (True, 0, False, ())
    assert type(receipt) is d.BusyReceipt
    assert receipt.operation == operation and receipt.identity == controller.identity
    assert not d._registration.blocked and not d._registration.active and not d._registration.seen
    controller.busy = False  # Independently simulated existing capacity becomes free.
    with admission() as domain:
        run(domain);domain.drain();domain.cleanup_complete()
    assert controller.events == ['LEASED', 'PREPARED', 'RUNNING', 'DRAINING', 'FREE']


@pytest.mark.parametrize('failure', [d.DomainBusy(), OSError('private controller diagnostic'),
    TimeoutError('interrupted acquisition'), subprocess.TimeoutExpired('acquire', 1),
    InterruptedError('interrupted response'), EOFError('lost reply')])
@pytest.mark.parametrize('partial', [False, True])
def test_any_exceptional_acquisition_is_quarantined_by_fresh_nonce(controller, failure, partial):
    original = controller.acquire
    attempted, quarantined = [], []
    def acquire(operation, deadline, check):
        attempted.append(operation)
        if partial:
            original(operation, deadline, check)
        raise failure
    quarantine = controller.quarantine
    def quarantine_nonce(operation, deadline):
        quarantined.append(operation)
        quarantine(operation, deadline)
    controller.acquire, controller.quarantine = acquire, quarantine_nonce
    with pytest.raises(d.DomainUnavailable) as caught:
        with admission():
            pytest.fail('Refused/uncertain acquisition started native work')
    assert type(caught.value) is d.DomainUnavailable
    assert str(caught.value) == 'Native execution domain is unavailable'
    assert quarantined == attempted and len(attempted) == 1
    assert 'PREPARED' not in controller.events and 'RUNNING' not in controller.events
    assert 'FREE' not in controller.events and d._registration.blocked
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Uncertain registration reused')
    assert len(attempted) == 1


def test_ambiguous_acquisition_quarantine_failure_latches_without_time_reset(controller, monkeypatch):
    original = controller.acquire
    attempts, quarantines = [], []
    def acquire(operation, deadline, check):
        attempts.append(operation)
        original(operation, deadline, check)
        raise d.DomainBusy()
    def quarantine(operation, deadline):
        quarantines.append(operation)
        raise OSError('controller unavailable')
    controller.acquire, controller.quarantine = acquire, quarantine
    with pytest.raises(d.DomainUnavailable) as caught:
        with admission():
            pytest.fail('Uncertain admission')
    assert type(caught.value) is d.DomainUnavailable
    assert attempts == quarantines and len(attempts) == 1
    monkeypatch.setattr(d.time, 'monotonic', lambda: 10**12)
    for _ in range(2):
        with pytest.raises(d.DomainUnavailable):
            with admission():
                pytest.fail('Time/reset bypass')
    assert len(attempts) == 1 and d._registration.blocked
    assert controller.events == ['LEASED']  # External cleanup remains unverified.


@pytest.mark.parametrize('kind', ['nonce', 'identity', 'subclass', 'lookalike', 'boolean', 'none'])
def test_busy_receipt_requires_exact_type_and_current_binding(controller, kind):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Lookalike:
        identity: d.Identity
        operation: str
    class Subclass(d.BusyReceipt):
        pass
    def acquire(operation, deadline, check):
        if kind == 'nonce':
            return d.BusyReceipt(controller.identity, 'stale-nonce')
        if kind == 'identity':
            return d.BusyReceipt(replace(controller.identity, epoch='stale'), operation)
        if kind == 'subclass':
            return Subclass(controller.identity, operation)
        if kind == 'lookalike':
            return Lookalike(controller.identity, operation)
        return True if kind == 'boolean' else None
    controller.acquire = acquire
    with pytest.raises(d.DomainUnavailable) as caught:
        with admission():
            pytest.fail('Malformed refusal')
    assert type(caught.value) is d.DomainUnavailable
    assert d._registration.blocked and controller.events == ['QUARANTINED']


def test_busy_receipt_from_previous_attempt_is_not_reusable(controller):
    receipts = []
    def acquire(operation, deadline, check):
        if not receipts:
            receipts.append(d.BusyReceipt(controller.identity, operation))
        return receipts[0]
    controller.acquire = acquire
    with pytest.raises(d.DomainBusy):
        with admission():
            pytest.fail('Capacity refusal started work')
    assert not d._registration.blocked
    with pytest.raises(d.DomainUnavailable) as caught:
        with admission():
            pytest.fail('Stale busy receipt accepted')
    assert type(caught.value) is d.DomainUnavailable
    assert d._registration.blocked and controller.events == ['QUARANTINED']


@pytest.mark.parametrize('kind', ['nonce', 'identity', 'old-domain', 'cross-slot'])
def test_domain_identity_cannot_be_substituted(controller, kind):
    original = controller.acquire
    def acquire(*args):
        lease = original(*args)
        if kind == 'nonce':
            return replace(lease, operation='another-operation')
        if kind == 'identity':
            return replace(lease, identity=replace(lease.identity, epoch='another-epoch'))
        if kind == 'cross-slot':
            return replace(lease, domain=d.domain_identity(
                lease.identity, lease.operation, 'slot-2', lease.generation))
        return replace(lease, domain=d.domain_identity(
            lease.identity, 'previous-operation', lease.slot, lease.generation))
    controller.acquire = acquire
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Identity substitution')
    assert controller.events == ['LEASED', 'QUARANTINED'] and d._registration.blocked


@pytest.mark.parametrize('cross_slot', [False, True])
def test_old_domain_rejected_with_increased_generation_or_another_slot(controller, cross_slot):
    first_domain = None
    if cross_slot:
        caps = controller.capabilities
        controller.capabilities = lambda deadline: replace(caps(deadline), slots=('slot-1', 'slot-2'))
        d._registration.expected_slots = ('slot-1', 'slot-2')
    with admission() as domain:
        first_domain = domain.lease.domain
        run(domain);domain.drain();domain.cleanup_complete()
    original = controller.acquire
    def acquire(*args):
        lease = original(*args)
        return replace(lease, domain=first_domain, slot='slot-2' if cross_slot else lease.slot)
    controller.acquire = acquire
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Old identity reused')
    assert controller.events.count('RUNNING') == 1 and d._registration.blocked


def test_repeated_generation_with_new_nonce_and_new_identity_is_rejected(controller):
    leases = []
    original = controller.acquire
    def acquire(*args):
        lease = original(*args)
        leases.append(lease)
        return lease
    controller.acquire = acquire
    with admission() as domain:
        run(domain);domain.drain();domain.cleanup_complete()
    controller.generation = 0
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Generation reused')
    assert leases[0].operation != leases[1].operation
    assert leases[0].domain != leases[1].domain
    assert leases[0].generation == leases[1].generation == 1
    assert d._registration.blocked


def test_completed_leases_have_fresh_exactly_bound_domains_and_bounded_history(controller):
    leases = []
    for _ in range(3):
        with admission() as domain:
            lease = domain.lease
            assert lease.domain == d.domain_identity(lease.identity, lease.operation, lease.slot, lease.generation)
            leases.append(lease)
            run(domain);domain.drain();domain.cleanup_complete()
    assert len({lease.domain for lease in leases}) == 3
    assert len({lease.operation for lease in leases}) == 3
    assert [lease.generation for lease in leases] == [1, 2, 3]
    assert d._registration.seen == {'slot-1': 3} and not d._registration.active


@pytest.mark.parametrize('new_epoch', [False, True])
def test_replacing_registration_cannot_reconcile_unresolved_controller_state(controller, monkeypatch, new_epoch):
    # Simulated external durable recovery authority, not local-memory recovery.
    first = d.Operation(d._registration)
    assert first.lease is not None and controller.busy
    prior_lease = first.lease
    controller.dirty = True
    if new_epoch:
        controller.identity = replace(controller.identity, epoch='epoch-2')
    monkeypatch.setattr(d, '_registration', d.Registration(
        controller, controller.identity, controller.limits, ('slot-1',)))
    before = list(controller.events)
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Local registration treated as recovery')
    assert controller.events == before and controller.busy and controller.dirty
    assert prior_lease.operation == first.operation_id and d._registration.blocked


def test_changed_epoch_on_existing_registration_requires_independent_recovery(controller):
    with admission() as domain:
        run(domain);domain.drain();domain.cleanup_complete()
    controller.identity = replace(controller.identity, epoch='epoch-2')
    with pytest.raises(d.DomainUnavailable):
        with admission():
            pytest.fail('Unpinned epoch')
    assert controller.events.count('LEASED') == 1 and d._registration.blocked


@pytest.mark.parametrize('atomic', [True, False])
def test_native_capacity_mapping_distinguishes_receipt_from_bare_busy(controller, monkeypatch, tmp_path, atomic):
    from app.core import native_isolation as boundary
    from app.core.ocr_v2.errors import EngineUnavailableError, OCRTimeoutError
    controller.busy = True
    if not atomic:
        def acquire(*args):
            raise d.DomainBusy()
        controller.acquire = acquire
    monkeypatch.setattr(boundary, 'run_hardened_subprocess', lambda *a, **kw: pytest.fail('Legacy child'))
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(tmp_path))
    with pytest.raises(OCRTimeoutError if atomic else EngineUnavailableError):
        boundary.execute('inspect_pdf', 'unused.pdf')
    assert not list(tmp_path.iterdir()) and not d._registration.active
    assert d._registration.blocked is not atomic


@pytest.mark.parametrize('atomic', [True, False])
def test_metadata_http_capacity_mapping_distinguishes_atomic_busy(controller, monkeypatch, tmp_path, atomic):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.tools.render import router, service, session
    controller.busy = True
    if not atomic:
        def acquire(*args):
            raise d.DomainBusy()
        controller.acquire = acquire
    root = tmp_path / 'sessions';root.mkdir()
    monkeypatch.setattr(session, 'SESSION_ROOT', root)
    manager = session.RenderSessionManager()
    monkeypatch.setattr(service, 'session_manager', manager)
    app = FastAPI();app.include_router(router.router)
    with TestClient(app) as client:
        response = client.post('/api/v1/render/sessions', files={
            'file': ('synthetic.pdf', b'%PDF-1.4\nsynthetic', 'application/pdf')})
    assert response.status_code == (429 if atomic else 503)
    assert response.json()['detail']['code'] == ('SESSION_METADATA_BUSY' if atomic else 'SESSION_METADATA_UNAVAILABLE')
    assert not manager._sessions and not list(root.iterdir())
    assert 'PREPARED' not in controller.events and 'RUNNING' not in controller.events
    assert d._registration.blocked is not atomic


class HandoffController(FakeController):
    """Synthetic server commits FREE before delivering its release response."""
    def __init__(self, slots):
        super().__init__()
        self.slots = slots
        self.server_lock = Lock()
        self.allocated = {}
        self.generations = {slot: 0 for slot in slots}
        self.release_started, self.allow_release = Event(), Event()
        self.lease_returned, self.allow_acquire = Event(), Event()
        self.allow_acquire.set()
        self.release_error = False
        self.release_response = None
        self.force_grant = False
        self.next_slot = None
        self.quarantined = []
        self.leases = []
        self.acquire_gates = {}
        self.acquire_events = {}

    def capabilities(self, deadline):
        return replace(super().capabilities(deadline), slots=self.slots)

    def acquire(self, operation, deadline, check):
        check()
        with self.server_lock:
            occupied = {lease.slot for lease in self.allocated.values()}
            slot = self.next_slot
            self.next_slot = None
            if slot is None:
                slot = next((s for s in self.slots if s not in occupied), None)
            if self.force_grant:
                slot = self.slots[0]
            if slot is None:
                return d.BusyReceipt(self.identity, operation)
            self.generations[slot] += 1
            generation = self.generations[slot]
            lease = d.Lease(self.identity, operation, slot, generation,
                            d.domain_identity(self.identity, operation, slot, generation))
            self.allocated[operation] = lease
            self.leases.append(lease)
            event = self.acquire_events.get(current_thread().name)
            if event is not None:
                event.set()
        if slot == 'slot-1' and generation > 1:
            self.lease_returned.set()
            assert self.acquire_gates.get(current_thread().name, self.allow_acquire).wait(2)
        return lease

    def release(self, lease, deadline):
        with self.server_lock:
            assert self.allocated.pop(lease.operation) == lease
        if lease.slot == 'slot-1' and lease.generation == 1:
            self.release_started.set()
            assert self.allow_release.wait(2)
            if self.release_error:
                raise OSError('synthetic lost release reply')
        return self.release_response

    def quarantine(self, operation, deadline):
        with self.server_lock:
            self.quarantined.append(operation)
            self.allocated.pop(operation, None)
        self.dirty = True


class ObservedHandoffCondition(Condition):
    """Observe condition entry without adding a hook to application code."""
    def __init__(self, lock, progress):
        super().__init__(lock)
        self.progress = progress
        self.waits = Queue()

    def wait(self, timeout=None):
        self.waits.put(current_thread().name)
        self.progress.set()
        return super().wait(timeout)


@pytest.fixture
def handoff(monkeypatch):
    class Rig:
        def __init__(self):
            self.controller = HandoffController(('slot-1', 'slot-2'))
            c = self.controller
            self.registration = d.Registration(c, c.identity, c.limits, c.slots)
            self.progress = Event()
            self.registration.condition = ObservedHandoffCondition(self.registration.lock, self.progress)
            self.threads, self.errors, self.admitted = [], [], []

        def prepared(self):
            operation = d.Operation(self.registration)
            run(operation);operation.drain();operation.cleanup_complete()
            return operation

        def start(self, name, operation=None, check=lambda: None, complete=True):
            done = Event()
            def work():
                domain = operation
                try:
                    if domain is None:
                        domain = d.Operation(self.registration, check)
                        self.admitted.append(domain)
                        if complete:
                            run(domain);domain.drain();domain.cleanup_complete()
                    if complete:
                        domain.finish(False)
                except BaseException as exc:
                    self.errors.append(exc)
                    if domain is not None and domain.state not in ('FREE', 'QUARANTINED'):
                        domain.finish(True)
                finally:
                    done.set();self.progress.set()
            thread = Thread(target=work, name=name)
            self.threads.append(thread);thread.start()
            return done

        def join(self):
            for thread in self.threads:
                thread.join(3)
                assert not thread.is_alive()

    rig = Rig()
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    monkeypatch.setattr(d, '_registration', rig.registration)
    try:
        yield rig
    finally:
        rig.controller.allow_release.set();rig.controller.allow_acquire.set()
        for gate in rig.controller.acquire_gates.values():
            gate.set()
        rig.join()


def test_legitimate_concurrent_release_handoff_does_not_quarantine(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    handoff.start('admitting-B')
    assert handoff.progress.wait(2)
    assert c.lease_returned.is_set()
    later = c.leases[-1]
    assert later.slot == first.lease.slot and later.operation != first.lease.operation
    assert later.generation > first.lease.generation
    assert later.domain == d.domain_identity(c.identity, later.operation, later.slot, later.generation)
    c.allow_release.set()
    handoff.join()
    assert not handoff.errors
    assert first.state == 'FREE' and handoff.admitted[0].state == 'FREE'
    assert not r.blocked and not r.active and not c.allocated and not c.quarantined


def wait_for_handoff(handoff):
    assert handoff.registration.condition.waits.get(timeout=2) == 'admitting-B'
    assert not handoff.admitted
    assert handoff.controller.events.count('RUNNING') == 1


def test_release_finalized_just_before_later_lease_validation(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    c.allow_acquire.clear()
    handoff.start('admitting-B')
    assert c.lease_returned.wait(2)
    c.allow_release.set()
    handoff.threads[0].join(2)
    assert first.state == 'FREE' and not r.active
    c.allow_acquire.set()
    handoff.join()
    assert not handoff.errors and handoff.admitted[0].state == 'FREE'
    assert not r.blocked and not r.releasing and not c.allocated


def test_later_lease_waits_until_release_rpc_acknowledged(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    handoff.start('admitting-B')
    wait_for_handoff(handoff)
    assert first.state == 'RELEASING'
    assert r.active == {'slot-1': first.operation_id}
    assert r.releasing == {'slot-1': first.lease}
    assert not r.blocked and not c.quarantined
    c.allow_release.set()
    handoff.join()
    assert not handoff.errors and first.state == 'FREE'
    assert handoff.admitted[0].state == 'FREE' and not r.active and not r.releasing


@pytest.mark.parametrize('state', ['RUNNING', 'DRAINING'])
def test_genuine_overlapping_grant_still_quarantines(handoff, state):
    c, r = handoff.controller, handoff.registration
    first = d.Operation(r)
    run(first)
    if state == 'DRAINING':
        first.drain()
    assert first.state == state
    c.force_grant = True
    with pytest.raises(d.DomainUnavailable):
        d.Operation(r)
    assert r.blocked and not r.releasing
    assert c.events.count('RUNNING') == 1
    assert c.quarantined == [c.leases[-1].operation]
    first.finish(True)
    assert not c.allocated


@pytest.mark.parametrize('fault', ['exception', 'unverified-response', 'quarantine-exception'])
def test_release_failure_wakes_waiting_admission_and_refuses_reuse(handoff, fault):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    done = handoff.start('admitting-B')
    wait_for_handoff(handoff)
    c.release_error = fault in ('exception', 'quarantine-exception')
    c.release_response = True if fault == 'unverified-response' else None
    if fault == 'quarantine-exception':
        original = c.quarantine
        def quarantine(operation, deadline):
            original(operation, deadline)
            raise OSError('synthetic lost quarantine reply')
        c.quarantine = quarantine
    c.allow_release.set()
    assert done.wait(2)
    handoff.join()
    assert len(handoff.errors) == 2
    assert all(type(error) is d.DomainUnavailable for error in handoff.errors)
    assert r.blocked and first.state == 'QUARANTINED' and not handoff.admitted
    assert r.active['slot-1'] == first.operation_id and r.releasing['slot-1'] == first.lease
    assert c.events.count('RUNNING') == 1 and not c.allocated
    with pytest.raises(d.DomainUnavailable):
        d.Operation(r)


def test_late_release_response_cannot_finalize_local_ownership(handoff, monkeypatch):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    original = c.release
    c.allow_release.set()
    def release(lease, deadline):
        response = original(lease, deadline)
        monkeypatch.setattr(d.time, 'monotonic', lambda: deadline + 1)
        return response
    c.release = release
    with pytest.raises(d.DomainUnavailable):
        first.finish(False)
    assert first.state == 'QUARANTINED' and r.blocked
    assert r.active == {'slot-1': first.operation_id} and r.releasing == {'slot-1': first.lease}


def test_unresolved_handoff_timeout_is_bounded_and_quarantined(handoff, monkeypatch):
    c, r = handoff.controller, handoff.registration
    monkeypatch.setattr(d, 'HANDOFF_SECONDS', 0.05)
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    done = handoff.start('admitting-B')
    wait_for_handoff(handoff)
    assert done.wait(2)  # Deadline, without releasing A or using a scheduling sleep.
    assert r.blocked and not handoff.admitted and first.state == 'RELEASING'
    assert c.leases[-1].operation in c.quarantined
    c.allow_release.set();handoff.join()
    assert first.state == 'QUARANTINED' and not c.allocated


def test_waiter_cancellation_runs_callback_without_lock_and_quarantines(handoff):
    c, r = handoff.controller, handoff.registration
    cancelled = Event()
    class Cancelled(Exception):
        pass
    error = Cancelled('synthetic handoff cancellation')
    def check():
        assert r.lock.acquire(blocking=False), 'Callback called under global lock'
        r.lock.release()
        if cancelled.is_set():
            raise error
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    done = handoff.start('admitting-B', check=check)
    wait_for_handoff(handoff)
    with r.condition:
        cancelled.set();r.condition.notify_all()
    assert done.wait(2)
    assert error in handoff.errors and r.blocked and not handoff.admitted
    assert c.leases[-1].operation in c.quarantined and c.events.count('RUNNING') == 1
    c.allow_release.set();handoff.join()
    assert not c.allocated


def test_spurious_notifications_cannot_complete_unconfirmed_handoff(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    handoff.start('admitting-B')
    for _ in range(3):
        wait_for_handoff(handoff)
        with r.condition:
            r.condition.notify_all()
    wait_for_handoff(handoff)
    assert r.active == {'slot-1': first.operation_id} and not r.blocked
    c.allow_release.set();handoff.join()
    assert not handoff.errors and not r.blocked and not r.active


def test_competing_faulty_grant_cannot_create_duplicate_local_ownership(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    done = handoff.start('admitting-B', complete=False)
    wait_for_handoff(handoff)
    gate = Event();c.acquire_gates['competing-C'] = gate
    acquired = Event();c.acquire_events['competing-C'] = acquired
    c.force_grant = True
    handoff.start('competing-C', complete=False)
    # Synchronize on C's acquired lease before allowing the original release.
    assert acquired.wait(2)
    c.allow_release.set()
    assert done.wait(2) and len(handoff.admitted) == 1
    winner = handoff.admitted[0]
    assert r.active == {'slot-1': winner.operation_id}
    gate.set();handoff.join()
    assert len(handoff.admitted) == 1 and len(handoff.errors) == 1
    assert type(handoff.errors[0]) is d.DomainUnavailable and r.blocked
    winner.finish(True)
    assert not c.allocated


def test_independent_slot_progresses_during_controller_release_and_handoff_wait(handoff):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    handoff.start('admitting-B')
    wait_for_handoff(handoff)
    c.next_slot = 'slot-2'
    other = handoff.prepared()
    other.finish(False)
    assert other.state == 'FREE' and first.state == 'RELEASING'
    assert not r.blocked and not handoff.admitted
    c.allow_release.set();handoff.join()
    assert not handoff.errors and not r.active and not r.releasing and not c.allocated


@pytest.mark.parametrize('change', ['epoch', 'policy', 'controller', 'generation', 'owner'])
def test_waiting_admission_revalidates_controller_policy_and_ownership(handoff, change):
    c, r = handoff.controller, handoff.registration
    first = handoff.prepared()
    handoff.start('releasing-A', first)
    assert c.release_started.wait(2)
    handoff.start('admitting-B')
    wait_for_handoff(handoff)
    with r.condition:
        if change == 'epoch':
            c.identity = replace(c.identity, epoch='changed-epoch')
        elif change == 'policy':
            c.limits = replace(c.limits, tasks=8)
        elif change == 'controller':
            r.controller = object()
        elif change == 'generation':
            r.seen['slot-1'] = 2
        else:
            r.active['slot-1'] = 'unexpected-owner'
        r.condition.notify_all()
    c.allow_release.set();handoff.join()
    assert r.blocked and not handoff.admitted
    assert any(type(error) is d.DomainUnavailable for error in handoff.errors)
    assert c.events.count('RUNNING') == 1 and not c.allocated

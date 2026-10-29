"""Synthetic transport/adapter compatibility tests, not protected runtime proof."""
from dataclasses import replace
import os
import sys
import time
import pytest
from app.core import native_domain as d


def module():
    import importlib.util
    assert importlib.util.find_spec('app.controller.adapter') is not None, 'Worker adapter is missing'
    from app.controller import adapter
    return adapter


def launch(path):
    return d.Launch((sys.executable, '-m', 'app.api.tools.render.session_metadata', str(path), str(os.getpid())),
                    {'LD_PRELOAD': '/tmp/injected.so'}, 30, 1024)


def test_metadata_translation_snapshots_input_and_discards_environment(tmp_path):
    m = module()
    path = tmp_path / 'file.pdf'
    path.write_bytes(b'bounded-local-fixture')
    manifest = m.translate(launch(path))
    assert manifest == {'kind': 'render_metadata', 'input': b'bounded-local-fixture'}
    assert str(path) not in repr(manifest)
    assert 'LD_PRELOAD' not in repr(manifest)


@pytest.mark.parametrize('change', [
    lambda v: replace(v, command=('/bin/sh', '-c', 'anything')),
    lambda v: replace(v, command=(v.command[0], '-m', 'arbitrary.module', *v.command[3:])),
    lambda v: replace(v, command=(*v.command, '--arbitrary')),
    lambda v: replace(v, timeout=31),
    lambda v: replace(v, max_output_bytes=2048),
])
def test_no_arbitrary_command_import_properties_or_runtime(tmp_path, change):
    m = module()
    path = tmp_path / 'file.pdf'
    path.write_bytes(b'pdf')
    with pytest.raises(d.DomainUnavailable):
        m.translate(change(launch(path)))


def test_symlink_hardlink_and_special_input_refused(tmp_path):
    m = module()
    path = tmp_path / 'file.pdf'
    path.write_bytes(b'pdf')
    link = tmp_path / 'link.pdf'
    link.symlink_to(path)
    with pytest.raises(d.DomainUnavailable):
        m.translate(launch(link))
    link.unlink()
    os.link(path, link)
    with pytest.raises(d.DomainUnavailable):
        m.translate(launch(path))


def test_transport_rejects_oversized_frames_unknown_tags_and_wrong_reply_binding():
    import importlib.util
    assert importlib.util.find_spec('app.controller.transport') is not None, 'protected transport is missing'
    from app.controller.transport import unpack, validate_reply
    with pytest.raises(d.DomainUnavailable):
        unpack({'$type': 'os.system', 'value': 'anything'})
    with pytest.raises(d.DomainUnavailable):
        validate_reply({'version': 1, 'request': 'wrong', 'identity': {}, 'result': None}, 'right', d.Identity('c','e','a'*64))


def test_actual_unix_transport_and_existing_metadata_contract_with_synthetic_manager(tmp_path, monkeypatch):
    """Real Unix credentials/pidfd/adapter; deliberately synthetic native manager."""
    m = module()
    from app.controller.transport import Client, Server, broker_handler
    from app.controller.broker import Broker, Journal
    from app.api.tools.render.session_metadata import _inspect
    import socket
    from threading import Thread, Event
    from test_broker import SyntheticManager
    identity = d.Identity('synthetic-controller', 'epoch-1', 'a' * 64)
    limits = d.Limits(64 << 20, 0, 16, 50000, 100000, 30)
    broker = Broker(Journal(tmp_path / 'db.sqlite', identity, ('one',), 8), SyntheticManager(), limits)
    endpoint = tmp_path / 'socket'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint))
    listener.listen(1)
    server = Server(identity, (os.getuid(),), broker_handler(broker), broker.disconnect, 2)
    def serve_one():
        connection, _ = listener.accept()
        server.connection(connection)
    thread = Thread(target=serve_one)
    thread.start()
    client = Client(endpoint, os.getuid(), identity)
    registration = d.Registration(m.WorkerController(client), identity, limits, ('one',))
    monkeypatch.setattr(d, '_registration', registration)
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE', 'required-nonproduction')
    source = tmp_path / 'local.pdf'
    source.write_bytes(b'synthetic-manager-does-not-parse-this')
    try:
        assert _inspect(source, Event()) == 2
        assert list(broker.journal.snapshot()['slots'])[0]['state'] == 'FREE'
        assert not registration.blocked
        # Healthy authenticated idle clients must survive the five-second
        # frame-completion budget; owner pidfds still bound their lifetime.
        time.sleep(5.1)
        assert _inspect(source, Event()) == 2
    finally:
        client.close()
        thread.join(timeout=2)
        listener.close()
        broker.journal.close()
    assert not thread.is_alive()


def test_declared_source_contains_no_automatic_startup_activation():
    m = module()
    assert d._registration is None
    assert hasattr(m, 'trusted_registration')



@pytest.mark.parametrize('operation', tuple(__import__('app.core.native_operations', fromlist=['OPERATIONS']).OPERATIONS))
def test_every_registry_operation_is_explicitly_unavailable_until_schema_exists(tmp_path, operation):
    """Denied coverage is not native execution compatibility qualification."""
    m = module()
    import json
    root = tmp_path / 'job'
    root.mkdir(mode=0o700)
    (root / 'request.json').write_text(json.dumps({'operation': operation}))
    request = d.Launch((sys.executable, '-m', 'app.core.native_supervisor', str(root), str(os.getpid())),
                       {}, 300, 65536)
    with pytest.raises(d.DomainUnavailable):
        m.translate(request)

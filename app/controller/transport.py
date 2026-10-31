"""Bounded authenticated Unix transport for the Worker and protected bridge.

Listeners are supplied by operator startup. This module installs no sockets,
services or permissions. Sessions pin SO_PEERCRED and a live pidfd; no wire
field selects an owner, identity, command, environment or systemd properties.
"""
from __future__ import annotations
import base64
from dataclasses import fields, is_dataclass
import json
import math
import os
from pathlib import Path
import secrets
import select
import socket
import struct
import threading
import time

from app.core import native_domain as d
from app.controller.linux import pidfd_open, alive
from app.controller.policy import MAX_INPUT, MAX_WIRE

_TYPES = {cls.__name__: cls for cls in (d.Identity, d.Limits, d.Capabilities, d.Lease,
                                        d.BusyReceipt, d.Membership, d.Drain)}


def pack(value, depth=0):
    if depth > 60:
        raise d.DomainUnavailable()
    nested = lambda x: pack(x, depth + 1)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is bytes and len(value) <= MAX_INPUT:
        return {'$type': 'bytes', 'value': base64.b64encode(value).decode('ascii')}
    if type(value) is tuple:
        return {'$type': 'tuple', 'value': [nested(x) for x in value]}
    if type(value) is list:
        return [nested(x) for x in value]
    if type(value) is dict and all(type(key) is str for key in value):
        if '$type' in value:
            raise d.DomainUnavailable()
        return {key: nested(item) for key, item in value.items()}
    if is_dataclass(value) and type(value).__name__ in _TYPES and type(value) is _TYPES[type(value).__name__]:
        return {'$type': type(value).__name__, 'value': {f.name: nested(getattr(value, f.name)) for f in fields(value)}}
    raise d.DomainUnavailable()


def unpack(value, depth=0):
    if depth > 60:
        raise d.DomainUnavailable()
    nested = lambda x: unpack(x, depth + 1)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [nested(x) for x in value]
    if type(value) is not dict:
        raise d.DomainUnavailable()
    if '$type' not in value:
        return {key: nested(item) for key, item in value.items()}
    if set(value) != {'$type', 'value'} or type(value['$type']) is not str:
        raise d.DomainUnavailable()
    kind, data = value['$type'], value['value']
    try:
        if kind == 'bytes' and type(data) is str and len(data) <= 4 * ((MAX_INPUT + 2) // 3):
            result = base64.b64decode(data, validate=True)
            if len(result) <= MAX_INPUT:
                return result
        if kind == 'tuple' and type(data) is list:
            return tuple(nested(x) for x in data)
        if kind in _TYPES and type(data) is dict and set(data) == {f.name for f in fields(_TYPES[kind])}:
            return _TYPES[kind](**{key: nested(item) for key, item in data.items()})
    except (ValueError, TypeError) as exc:
        raise d.DomainUnavailable() from exc
    raise d.DomainUnavailable()


def _receive(sock, deadline):
    def exact(length):
        chunks = bytearray()
        while len(chunks) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise d.DomainUnavailable()
            sock.settimeout(remaining)
            chunk = sock.recv(min(length - len(chunks), 65536))
            if not chunk:
                raise d.DomainUnavailable()
            chunks.extend(chunk)
        return bytes(chunks)
    length = struct.unpack('!I', exact(4))[0]
    if not 0 < length <= MAX_WIRE:
        raise d.DomainUnavailable()
    try:
        return unpack(json.loads(exact(length)))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise d.DomainUnavailable() from exc


def _send(sock, value, deadline):
    data = json.dumps(pack(value), separators=(',', ':'), allow_nan=False).encode()
    if not 0 < len(data) <= MAX_WIRE or time.monotonic() >= deadline:
        raise d.DomainUnavailable()
    sock.settimeout(deadline - time.monotonic())
    sock.sendall(struct.pack('!I', len(data)) + data)


def credentials(sock, expected_uids):
    pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid <= 0 or uid not in expected_uids:
        raise d.DomainUnavailable()
    fd = pidfd_open(pid)
    if not alive(fd):
        os.close(fd)
        raise d.DomainUnavailable()
    return pid, uid, gid, fd


def validate_reply(reply, request, identity):
    if (type(reply) is not dict or set(reply) not in (
            {'version', 'request', 'identity', 'result'}, {'version', 'request', 'identity', 'error'})
            or reply['version'] != 1 or reply['request'] != request or reply['identity'] != identity):
        raise d.DomainUnavailable()
    if 'error' in reply:
        raise d.DomainUnavailable()  # No raw remote exception becomes public detail.
    return reply['result']


class Client:
    def __init__(self, path: Path, peer_uid: int, identity: d.Identity):
        self.identity, self.peer_uid = identity, peer_uid
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect(str(path))
            self.peer = credentials(self.sock, (peer_uid,))
        except BaseException:
            self.sock.close()
            raise d.DomainUnavailable() from None
        self.closed = False

    def request(self, method, deadline, **payload):
        with self.lock:
            try:
                if self.closed or not alive(self.peer[3]):
                    raise d.DomainUnavailable()
                request = secrets.token_hex(16)
                envelope = {'version': 1, 'request': request, 'identity': self.identity,
                            'method': method, 'payload': payload}
                _send(self.sock, envelope, deadline)
                result = validate_reply(_receive(self.sock, deadline), request, self.identity)
                if not alive(self.peer[3]):
                    raise d.DomainUnavailable()
                return result
            except BaseException:
                self.close()
                raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.sock.close()
            os.close(self.peer[3])


class Server:
    def __init__(self, identity, allowed_uids, handler, disconnected, max_connections):
        if not 0 < max_connections <= 68:
            raise d.DomainUnavailable()
        self.identity, self.allowed_uids = identity, tuple(allowed_uids)
        self.handler, self.disconnected = handler, disconnected
        self.capacity = threading.BoundedSemaphore(max_connections)

    def connection(self, sock, reserved=False):
        session, peer = secrets.token_hex(32), None
        admitted = reserved or self.capacity.acquire(blocking=False)
        if not admitted:
            sock.close()
            return
        try:
            peer = credentials(sock, self.allowed_uids)
            for _ in range(8192):  # Finite session control/event budget.
                if not alive(peer[3]):
                    raise d.DomainUnavailable()
                # Idle lifetime is tied to the authenticated owner's pidfd.
                # The five-second budget bounds completion of a started frame,
                # rather than disconnecting every healthy idle Worker/broker.
                while not select.select([sock], [], [], 1)[0]:
                    if not alive(peer[3]):
                        raise d.DomainUnavailable()
                message = _receive(sock, time.monotonic() + 5)
                if (type(message) is not dict or set(message) != {'version', 'request', 'identity', 'method', 'payload'}
                        or message['version'] != 1 or message['identity'] != self.identity
                        or type(message['request']) is not str or len(message['request']) != 32
                        or type(message['method']) is not str or type(message['payload']) is not dict):
                    raise d.DomainUnavailable()
                response = {'version': 1, 'request': message['request'], 'identity': self.identity}
                try:
                    response['result'] = self.handler(message['method'], message['payload'], peer[0], session)
                except Exception:
                    response['error'] = 'CONTROLLER_UNAVAILABLE'
                if not alive(peer[3]):
                    raise d.DomainUnavailable()
                _send(sock, response, time.monotonic() + 5)
        except (OSError, d.DomainUnavailable, ValueError):
            pass
        finally:
            sock.close()
            try:
                if peer is not None:
                    os.close(peer[3])
                    self.disconnected(session)
            finally:
                self.capacity.release()

    def serve(self, listener):
        """Consume an operator-supplied listening Unix socket; no provisioning."""
        if listener.family != socket.AF_UNIX:
            raise d.DomainUnavailable()
        while True:
            connection, _ = listener.accept()
            # Bound before thread creation as well as inside connection().
            if not self.capacity.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self.connection, args=(connection, True), daemon=True).start()


def broker_handler(broker):
    def handler(method, args, pid, session):
        signatures = {
            'capabilities': (), 'acquire': ('operation', 'wait'),
            'prepare': ('lease', 'manifest'), 'run': ('lease', 'token'),
            'poll': ('lease',), 'drain': ('lease',), 'ack': ('lease','sequence'),
            'release': ('lease', 'digest'), 'quarantine': ('operation',),
        }
        if method not in signatures or set(args) != set(signatures[method]):
            raise d.DomainUnavailable()
        deadline = time.monotonic() + 5
        if method == 'capabilities':
            return broker.capabilities()
        if method == 'acquire':
            return broker.acquire(args['operation'], pid, session, args['wait'])
        if method == 'quarantine':
            return broker.quarantine(args['operation'], session)
        lease = args['lease']
        if type(lease) is not d.Lease:
            raise d.DomainUnavailable()
        if method == 'prepare':
            return broker.prepare(lease, session, args['manifest'], deadline)
        if method == 'run':
            return broker.run(lease, session, args['token'], deadline)
        if method == 'ack':
            return broker.ack(lease, session, args['sequence'], deadline)
        if method == 'release':
            return broker.release(lease, session, args['digest'], deadline)
        return getattr(broker, method)(lease, session, deadline)
    return handler


def manager_handler(manager):
    def handler(method, args, pid, session):
        signatures = {'preflight': (), 'inventory': (), 'prepare': ('lease', 'manifest'),
                      'grant': ('lease', 'token'), 'poll': ('lease',), 'drain': ('lease',), 'ack': ('lease','sequence'),
                      'cleanup': ('lease',), 'abort': ('lease',)}
        if method not in signatures or set(args) != set(signatures[method]):
            raise d.DomainUnavailable()
        if method in ('preflight', 'inventory'):
            return getattr(manager, method)()
        if type(args['lease']) is not d.Lease:
            raise d.DomainUnavailable()
        return getattr(manager, method)(**args, deadline=time.monotonic() + 5)
    return handler


class ManagerClient:
    """The unprivileged broker sees only the restricted protected bridge API."""
    def __init__(self, client):
        self.client = client

    def preflight(self):
        return self.client.request('preflight', time.monotonic() + 5)

    def inventory(self):
        return self.client.request('inventory', time.monotonic() + 5)

    def prepare(self, lease, manifest, deadline):
        return self.client.request('prepare', deadline, lease=lease, manifest=manifest)

    def grant(self, lease, token, deadline):
        return self.client.request('grant', deadline, lease=lease, token=token)

    def poll(self, lease, deadline):
        return self.client.request('poll', deadline, lease=lease)

    def ack(self, lease, sequence, deadline):
        return self.client.request('ack', deadline, lease=lease, sequence=sequence)

    def drain(self, lease, deadline):
        return self.client.request('drain', deadline, lease=lease)

    def cleanup(self, lease, deadline):
        return self.client.request('cleanup', deadline, lease=lease)

    def abort(self, lease, deadline):
        return self.client.request('abort', deadline, lease=lease)

"""Explicit operator service wiring. No environment selection or auto-activation.

Callers must supply already provisioned Unix listeners, protected Policy/runtime,
separate identities and finite storage. These functions do not create or install
host users, sockets, services, profiles, cgroups or mounts.
"""
import os
from pathlib import Path

from app.core.native_domain import DomainUnavailable
from app.controller.broker import Broker, Journal
from app.controller.manager import SystemManager
from app.controller.transport import Client, ManagerClient, Server, broker_handler, manager_handler


def serve_manager(listener, policy):
    if os.geteuid() != 0 or policy is None:
        raise DomainUnavailable()
    manager = SystemManager(policy)
    manager.preflight()
    def disconnected(session):
        # Manager deadlines continue even if this best-effort stop is unavailable.
        import time
        for domain, record in list(manager.records.items()):
            try:
                manager.abort(record['lease'], time.monotonic() + 5)
            except BaseException:
                pass
    Server(policy.identity, (policy.broker_uid,), manager_handler(manager), disconnected, 2).serve(listener)


def serve_broker(listener, policy, journal_path, bridge_socket):
    if policy is None or os.geteuid() != policy.broker_uid:
        raise DomainUnavailable()
    policy.validate()
    bridge = Client(Path(bridge_socket), 0, policy.identity)
    journal = None
    try:
        journal = Journal(Path(journal_path), policy.identity, policy.slots)
        broker = Broker(journal, ManagerClient(bridge), policy.limits)
        Server(policy.identity, policy.client_uids, broker_handler(broker), broker.disconnect,
               min(68, len(policy.slots) + 4)).serve(listener)
    finally:
        bridge.close()
        if journal is not None:
            journal.close()

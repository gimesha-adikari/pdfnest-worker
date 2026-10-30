"""Startup construction refuses missing authority, with no host mutation."""
import importlib.util
import os
import pytest
from app.core.native_domain import DomainUnavailable


def test_service_factories_require_pinned_separate_identity(monkeypatch):
    assert importlib.util.find_spec('app.controller.startup') is not None, 'service wiring is missing'
    from app.controller.startup import serve_manager, serve_broker
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    with pytest.raises(DomainUnavailable):
        serve_manager(None, None)
    with pytest.raises(DomainUnavailable):
        serve_broker(None, None, None, None)

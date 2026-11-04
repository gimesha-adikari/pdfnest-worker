from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import replace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core import storage

client = TestClient(app)


def signed_get_headers(path: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = f"health-{uuid.uuid4().hex}"
    body_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n{path}\n{timestamp}\n{nonce}\n{body_hash}"
    signature = hmac.new(settings.worker_shared_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Worker-Signature": signature,
        "X-Worker-Timestamp": timestamp,
        "X-Worker-Nonce": nonce,
        "X-Worker-Body-Hash": body_hash,
    }


def test_health_endpoints():
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    res_live = client.get("/health/live")
    assert res_live.status_code == 200
    assert res_live.json() == {"status": "alive"}


def test_health_ready_success():
    with patch("redis.Redis.from_url") as mock_redis:
        mock_instance = MagicMock()
        mock_instance.ping.return_value = True
        mock_redis.return_value = mock_instance

        res_ready = client.get("/health/ready")
        assert res_ready.status_code == 200
        assert res_ready.json() == {"status": "ready"}


def test_health_ready_redis_down():
    with patch("redis.Redis.from_url") as mock_redis:
        mock_instance = MagicMock()
        mock_instance.ping.side_effect = Exception("Connection refused")
        mock_redis.return_value = mock_instance

        res_ready = client.get("/health/ready")
        assert res_ready.status_code == 503
        assert res_ready.json() == {"status": "not_ready"}


def test_public_readiness_response_is_sanitized(monkeypatch):
    monkeypatch.setattr("app.main.remote_storage_enabled", lambda: False)
    with patch("redis.Redis.from_url") as mock_redis, patch("app.main.shutil.which") as which:
        mock_redis.return_value.ping.return_value = True
        mock_redis.return_value.get.return_value = "fresh"
        which.side_effect = lambda binary: binary in {"tesseract", "gs", "soffice"}

        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_internal_readiness_requires_worker_auth_and_returns_sanitized_capabilities(monkeypatch):
    monkeypatch.setattr("app.main.remote_storage_enabled", lambda: False)
    unsigned = client.get("/internal/health/ready")
    assert unsigned.status_code == 401

    with patch("redis.Redis.from_url") as mock_redis, patch("app.main.shutil.which") as which:
        mock_redis.return_value.ping.return_value = True
        which.side_effect = lambda binary: binary in {"tesseract", "gs", "soffice"}
        signed = client.get("/internal/health/ready", headers=signed_get_headers("/internal/health/ready"))
    assert signed.status_code == 200
    body = signed.json()
    assert body["status"] == "ready"
    assert set(body["capabilities"]) == {"document_processing", "ocr", "office_to_pdf", "pdf_to_office"}
    assert all(type(value) is bool for value in body["capabilities"].values())
    assert not {"redis", "r2", "actor", "binaries", "reason"}.intersection(body)


def test_capabilities_distinguish_missing_ocr_engine_from_other_tools(monkeypatch):
    monkeypatch.setattr("app.main.remote_storage_enabled", lambda: False)
    with patch("redis.Redis.from_url") as mock_redis, patch("app.main.shutil.which") as which:
        mock_redis.return_value.ping.return_value = True
        which.side_effect = lambda binary: f"/usr/bin/{binary}" if binary in {"gs", "soffice"} else None
        response = client.get(
            "/internal/health/ready",
            headers=signed_get_headers("/internal/health/ready"),
        )

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "capabilities": {
            "document_processing": True,
            "ocr": False,
            "office_to_pdf": True,
            "pdf_to_office": True,
        },
    }


def test_health_prefix_does_not_bypass_worker_authentication():
    response = client.get("/health/private-probe")
    assert response.status_code == 401


def test_r2_readiness_client_uses_short_nonretrying_timeouts(monkeypatch):
    storage.get_r2_readiness_client.cache_clear()
    configured = replace(
        settings,
        r2_bucket="synthetic-bucket",
        r2_access_key="synthetic-access",
        r2_secret_key="synthetic-secret",
        r2_endpoint="https://synthetic.invalid",
    )
    monkeypatch.setattr(storage, "settings", configured)
    try:
        with patch("app.core.storage.boto3.client") as make_client:
            storage.get_r2_readiness_client()

        config = make_client.call_args.kwargs["config"]
        assert config.connect_timeout == 0.75
        assert config.read_timeout == 0.75
        assert config.retries["max_attempts"] == 0
    finally:
        storage.get_r2_readiness_client.cache_clear()

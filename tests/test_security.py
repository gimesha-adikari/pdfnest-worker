import hashlib
import hmac
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.core.config import settings

client = TestClient(app)
SECRET = settings.worker_shared_secret


def generate_headers(
    method: str,
    path: str,
    secret: str = SECRET,
    timestamp: float | None = None,
    nonce: str | None = None,
    body_hash: str | None = None,
) -> dict[str, str]:
    if timestamp is None:
        timestamp = time.time()
    if nonce is None:
        nonce = f"nonce-{uuid.uuid4().hex}"

    timestamp_str = str(int(timestamp))
    method_str = method.upper()

    if body_hash:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}\n{body_hash}"
    else:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}"

    sig = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-Worker-Signature": sig,
        "X-Worker-Timestamp": timestamp_str,
        "X-Worker-Nonce": nonce,
    }
    if body_hash:
        headers["X-Worker-Body-Hash"] = body_hash

    return headers


def test_public_routes_bypass_auth():
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/home").status_code == 200


def test_protected_route_missing_headers():
    response = client.post("/api/v1/metadata/read")
    assert response.status_code == 401
    assert "Missing worker authentication" in response.json()["detail"]


def test_protected_route_invalid_signature():
    headers = {
        "X-Worker-Signature": "invalidhexsignature123",
        "X-Worker-Timestamp": str(int(time.time())),
        "X-Worker-Nonce": f"nonce-invalid-sig-{uuid.uuid4().hex}",
    }
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401
    assert "Invalid worker authentication signature" in response.json()["detail"]


def test_protected_route_wrong_secret():
    headers = generate_headers(
        method="POST",
        path="/api/v1/metadata/read",
        secret="wrong-secret-key",
    )
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401
    assert "Invalid worker authentication signature" in response.json()["detail"]


def test_protected_route_modified_path():
    headers = generate_headers(
        method="POST",
        path="/api/v1/different/path",
    )
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401


def test_protected_route_modified_method():
    headers = generate_headers(
        method="GET",
        path="/api/v1/metadata/read",
    )
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401


def test_protected_route_expired_timestamp():
    expired_ts = time.time() - 600  # 10 minutes ago
    headers = generate_headers(
        method="POST",
        path="/api/v1/metadata/read",
        timestamp=expired_ts,
    )
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401
    assert "timestamp expired" in response.json()["detail"]


def test_protected_route_future_timestamp():
    future_ts = time.time() + 600  # 10 minutes in future
    headers = generate_headers(
        method="POST",
        path="/api/v1/metadata/read",
        timestamp=future_ts,
    )
    response = client.post("/api/v1/metadata/read", headers=headers)
    assert response.status_code == 401
    assert "timestamp expired" in response.json()["detail"]


def test_protected_route_reused_nonce():
    nonce = f"reused-nonce-unique-{uuid.uuid4().hex}"
    headers1 = generate_headers(
        method="POST",
        path="/api/v1/metadata/read",
        nonce=nonce,
    )
    # First attempt
    res1 = client.post("/api/v1/metadata/read", headers=headers1)
    assert res1.status_code != 401

    # Second attempt with same nonce
    headers2 = generate_headers(
        method="POST",
        path="/api/v1/metadata/read",
        nonce=nonce,
    )
    res2 = client.post("/api/v1/metadata/read", headers=headers2)
    assert res2.status_code == 401
    assert "nonce replayed" in res2.json()["detail"]


def test_invalid_signature_does_not_consume_valid_nonce(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    nonce = f"invalid-first-{uuid.uuid4().hex}"
    valid = generate_headers("POST", "/api/v1/metadata/read", nonce=nonce)
    invalid = dict(valid, **{"X-Worker-Signature": "0" * 64})
    assert client.post("/api/v1/metadata/read", headers=invalid).status_code == 401
    assert client.post("/api/v1/metadata/read", headers=valid).status_code != 401


@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf"])
def test_nonfinite_timestamp_is_rejected(timestamp, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    nonce = uuid.uuid4().hex
    signed = f"POST\n/api/v1/metadata/read\n{timestamp}\n{nonce}"
    headers = {
        "X-Worker-Timestamp": timestamp,
        "X-Worker-Nonce": nonce,
        "X-Worker-Signature": hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest(),
    }
    assert client.post("/api/v1/metadata/read", headers=headers).status_code == 401


def test_future_timestamp_nonce_outlives_full_acceptance_window(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    now = time.time()
    headers = generate_headers("POST", "/api/v1/metadata/read", timestamp=now + 290)
    monkeypatch.setattr("app.core.security.time.time", lambda: now)
    assert client.post("/api/v1/metadata/read", headers=headers).status_code != 401
    monkeypatch.setattr("app.core.security.time.time", lambda: now + 310)
    assert client.post("/api/v1/metadata/read", headers=headers).status_code == 401


def test_managed_replay_store_failure_is_closed(monkeypatch):
    from redis.asyncio import Redis

    def unavailable(*args, **kwargs):
        raise ConnectionError("controlled test outage")

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://audit.invalid")
    monkeypatch.setattr(Redis, "from_url", unavailable)
    response = client.post("/api/v1/metadata/read", headers=generate_headers("POST", "/api/v1/metadata/read"))
    assert response.status_code == 503

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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
        data = res_ready.json()
        assert data["status"] == "ready"
        assert data["redis"] is True
        assert "binaries" in data


def test_health_ready_redis_down():
    with patch("redis.Redis.from_url") as mock_redis:
        mock_instance = MagicMock()
        mock_instance.ping.side_effect = Exception("Connection refused")
        mock_redis.return_value = mock_instance

        res_ready = client.get("/health/ready")
        assert res_ready.status_code == 503
        data = res_ready.json()
        assert data["status"] == "not_ready"
        assert data["redis"] is False
        assert "reason" in data

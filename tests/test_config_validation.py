import pytest

from app.core.config import validate_runtime_config


def test_managed_worker_rejects_local_fallbacks(monkeypatch):
    monkeypatch.setenv("APP_ENV", "canary")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("R2_ENDPOINT", "https://r2.example.invalid")
    monkeypatch.setenv("R2_BUCKET", "canary")
    monkeypatch.setenv("R2_ACCESS_KEY", "access")
    monkeypatch.setenv("R2_SECRET_KEY", "secret")
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "12345678901234567890123456789012")
    monkeypatch.setenv("WORKER_SHARED_SECRET", "shared")
    with pytest.raises(ValueError, match="REDIS_URL"):
        validate_runtime_config()

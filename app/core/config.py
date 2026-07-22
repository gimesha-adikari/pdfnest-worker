from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "Platen PDF Worker"
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    app_env: str = os.getenv("APP_ENV", "development")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    job_ttl_seconds: int = int(os.getenv("JOB_TTL_SECONDS", "86400"))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",")
            if origin.strip()
        ]
    )

    r2_bucket: str = os.getenv("R2_BUCKET", "")
    r2_access_key: str = os.getenv("R2_ACCESS_KEY", "")
    r2_secret_key: str = os.getenv("R2_SECRET_KEY", "")
    r2_endpoint: str = os.getenv("R2_ENDPOINT", "")


settings = Settings()
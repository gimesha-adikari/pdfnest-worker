import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _parse_int_env(var_name: str, default: int, min_val: int, max_val: int) -> int:
    val_str = os.getenv(var_name, "").strip()
    if not val_str:
        return default
    try:
        val = int(val_str)
        return max(min_val, min(val, max_val))
    except ValueError:
        return default


def _parse_bool_env(var_name: str, default: bool = False) -> bool:
    val_str = os.getenv(var_name, "").strip().lower()
    if not val_str:
        return default
    return val_str in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_name: str = "Platen PDF Worker"
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    app_env: str = os.getenv("APP_ENV", "development")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    job_ttl_seconds: int = int(os.getenv("JOB_TTL_SECONDS", "86400"))
    stuck_job_timeout_seconds: int = int(os.getenv("STUCK_JOB_TIMEOUT_SECONDS", "1200"))
    global_heavy_execution_limit: int = int(os.getenv("GLOBAL_HEAVY_EXECUTION_LIMIT", os.getenv("MAX_CONCURRENT_HEAVY_JOBS", "4")))
    per_identity_heavy_execution_limit: int = int(os.getenv("PER_IDENTITY_HEAVY_EXECUTION_LIMIT", "2"))
    heavy_lease_ttl_seconds: int = int(os.getenv("HEAVY_LEASE_TTL_SECONDS", "600"))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",")
            if origin.strip()
        ]
    )

    r2_bucket: str = os.getenv("R2_BUCKET", "").strip('\'" ')
    r2_access_key: str = os.getenv("R2_ACCESS_KEY", "").strip('\'" ')
    r2_secret_key: str = os.getenv("R2_SECRET_KEY", "").strip('\'" ')
    r2_endpoint: str = os.getenv("R2_ENDPOINT", "").strip('\'" ')

    worker_shared_secret: str = os.getenv("WORKER_SHARED_SECRET", "dev-secret-change-in-production").strip()

    enable_persistent_render_pool: bool = field(default_factory=lambda: _parse_bool_env("ENABLE_PERSISTENT_RENDER_POOL", False))
    persistent_render_pool_size: int = field(default_factory=lambda: _parse_int_env("PERSISTENT_RENDER_POOL_SIZE", 4, 1, 32))
    worker_max_renders: int = field(default_factory=lambda: _parse_int_env("WORKER_MAX_RENDERS", 1000, 1, 100000))
    worker_max_rss_mb: int = field(default_factory=lambda: _parse_int_env("WORKER_MAX_RSS_MB", 350, 50, 4096))
    enable_render_failure_injection: bool = field(default_factory=lambda: _parse_bool_env("ENABLE_RENDER_FAILURE_INJECTION", False))


settings = Settings()
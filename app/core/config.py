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


MANAGED_ENVS = {"canary", "staging", "production"}


def is_managed_environment() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in MANAGED_ENVS


def remote_storage_enabled() -> bool:
    """Select remote storage only when explicitly requested locally.

    Managed environments always require R2. In local development, dotenv
    credentials alone must not redirect a run to a remote bucket; use
    STORAGE_MODE=r2 for an intentional local remote-storage smoke test.
    """
    if is_managed_environment():
        return True
    mode = os.getenv("STORAGE_MODE", "").strip().lower()
    return mode in {"r2", "s3", "remote"}


def validate_runtime_config() -> None:
    """Reject local fallbacks when a managed worker is starting."""
    if not is_managed_environment():
        return

    required = {
        "REDIS_URL": os.getenv("REDIS_URL", "").strip(),
        "R2_ENDPOINT": os.getenv("R2_ENDPOINT", "").strip(),
        "R2_BUCKET": os.getenv("R2_BUCKET", "").strip(),
        "R2_ACCESS_KEY": os.getenv("R2_ACCESS_KEY", "").strip(),
        "R2_SECRET_KEY": os.getenv("R2_SECRET_KEY", "").strip(),
        "FILE_ENCRYPTION_KEY": os.getenv("FILE_ENCRYPTION_KEY", "").strip(),
        "WORKER_SHARED_SECRET": os.getenv("WORKER_SHARED_SECRET", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"managed APP_ENV requires: {', '.join(missing)}")

    for name in ("REDIS_URL", "R2_ENDPOINT"):
        value = required[name].lower()
        if any(host in value for host in ("localhost", "127.0.0.1", "0.0.0.0")):
            raise ValueError(f"managed configuration {name} must not point to localhost or loopback")
    if len(required["FILE_ENCRYPTION_KEY"]) != 32:
        raise ValueError("managed FILE_ENCRYPTION_KEY must be exactly 32 characters")
    if os.getenv("APP_ENV", "").strip().lower() == "canary" and "canary" not in required["R2_BUCKET"].lower():
        raise ValueError("canary R2_BUCKET must be an explicitly canary-named dedicated bucket")
    heartbeat_key = os.getenv("ACTOR_HEARTBEAT_KEY", f"pdfnest:{os.getenv('APP_ENV', '').strip().lower()}:actor:heartbeat").strip()
    expected_prefix = f"pdfnest:{os.getenv('APP_ENV', '').strip().lower()}:"
    if not heartbeat_key.startswith(expected_prefix):
        raise ValueError("ACTOR_HEARTBEAT_KEY must be namespaced to APP_ENV")


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
    r2_connect_timeout_seconds: int = field(default_factory=lambda: _parse_int_env("R2_CONNECT_TIMEOUT_SECONDS", 5, 1, 30))
    r2_read_timeout_seconds: int = field(default_factory=lambda: _parse_int_env("R2_READ_TIMEOUT_SECONDS", 30, 5, 300))
    r2_max_attempts: int = field(default_factory=lambda: _parse_int_env("R2_MAX_ATTEMPTS", 3, 1, 5))

    worker_shared_secret: str = os.getenv("WORKER_SHARED_SECRET", "dev-secret-change-in-production").strip()

    enable_persistent_render_pool: bool = field(default_factory=lambda: _parse_bool_env("ENABLE_PERSISTENT_RENDER_POOL", False))
    persistent_render_pool_size: int = field(default_factory=lambda: _parse_int_env("PERSISTENT_RENDER_POOL_SIZE", 4, 1, 32))
    worker_max_renders: int = field(default_factory=lambda: _parse_int_env("WORKER_MAX_RENDERS", 1000, 1, 100000))
    worker_max_rss_mb: int = field(default_factory=lambda: _parse_int_env("WORKER_MAX_RSS_MB", 350, 50, 4096))
    enable_render_failure_injection: bool = field(default_factory=lambda: _parse_bool_env("ENABLE_RENDER_FAILURE_INJECTION", False))
    actor_heartbeat_required: bool = field(default_factory=lambda: _parse_bool_env("ACTOR_HEARTBEAT_REQUIRED", is_managed_environment()))
    actor_heartbeat_key: str = field(default_factory=lambda: os.getenv("ACTOR_HEARTBEAT_KEY", f"pdfnest:{os.getenv('APP_ENV', 'development')}:actor:heartbeat").strip())
    actor_heartbeat_ttl_seconds: int = field(default_factory=lambda: _parse_int_env("ACTOR_HEARTBEAT_TTL_SECONDS", 30, 5, 300))
    actor_heartbeat_interval_seconds: int = field(default_factory=lambda: _parse_int_env("ACTOR_HEARTBEAT_INTERVAL_SECONDS", 10, 1, 120))


settings = Settings()

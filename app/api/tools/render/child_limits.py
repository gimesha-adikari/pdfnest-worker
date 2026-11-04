"""Hard POSIX resource ceilings for the isolated native render child."""

from __future__ import annotations

import importlib

MAX_RENDER_CHILD_ADDRESS_SPACE = 512 * 1024 * 1024
MAX_RENDER_CHILD_CPU_SECONDS = 25
MAX_RENDER_CHILD_FILE_BYTES = 64 * 1024 * 1024


def _bounded_pair(resource_api, kind: int, soft_cap: int, hard_cap: int) -> tuple[int, int]:
    current_soft, current_hard = resource_api.getrlimit(kind)
    infinity = resource_api.RLIM_INFINITY
    effective_hard = hard_cap if current_hard == infinity else min(current_hard, hard_cap)
    effective_soft = soft_cap if current_soft == infinity else min(current_soft, soft_cap)
    effective_soft = min(effective_soft, effective_hard)
    return effective_soft, effective_hard


def apply_render_child_limits(resource_api=None) -> None:
    """Apply memory, CPU, and output-file ceilings; fail closed if unavailable."""
    if resource_api is None:
        try:
            resource_api = importlib.import_module("resource")
        except ImportError as exc:  # pragma: no cover - Worker images are Linux
            raise RuntimeError("hard render-child resource limits are unavailable") from exc

    required = ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIM_INFINITY", "getrlimit", "setrlimit")
    if any(not hasattr(resource_api, item) for item in required):
        raise RuntimeError("hard render-child resource limits are unavailable")

    limits = (
        (resource_api.RLIMIT_AS, MAX_RENDER_CHILD_ADDRESS_SPACE, MAX_RENDER_CHILD_ADDRESS_SPACE),
        (resource_api.RLIMIT_CPU, MAX_RENDER_CHILD_CPU_SECONDS, MAX_RENDER_CHILD_CPU_SECONDS),
        (resource_api.RLIMIT_FSIZE, MAX_RENDER_CHILD_FILE_BYTES, MAX_RENDER_CHILD_FILE_BYTES),
    )
    try:
        for kind, soft_cap, hard_cap in limits:
            bounded = _bounded_pair(resource_api, kind, soft_cap, hard_cap)
            resource_api.setrlimit(kind, bounded)
            applied = resource_api.getrlimit(kind)
            if applied[0] > soft_cap or applied[1] > hard_cap:
                raise RuntimeError("render-child resource limit did not take effect")
    except (OSError, ValueError) as exc:
        raise RuntimeError("hard render-child resource limits could not be applied") from exc

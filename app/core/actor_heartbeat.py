from __future__ import annotations

import logging
import os
import threading

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


def heartbeat_key() -> str:
    return settings.actor_heartbeat_key


def write_heartbeat() -> None:
    redis_client.set(
        heartbeat_key(),
        "1",
        ex=settings.actor_heartbeat_ttl_seconds,
    )


def start_actor_heartbeat() -> threading.Event | None:
    """Run only in the dedicated actor process, never in the HTTP API."""
    if not settings.actor_heartbeat_required or os.getenv("PDFNEST_ACTOR") != "true":
        return None

    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                write_heartbeat()
            except Exception as exc:
                logger.warning("[ACTOR HEARTBEAT] update failed: %s", exc)
            stop.wait(settings.actor_heartbeat_interval_seconds)

    thread = threading.Thread(target=loop, name="actor-heartbeat", daemon=True)
    thread.start()
    logger.info("[ACTOR HEARTBEAT] publishing %s", heartbeat_key())
    return stop

from __future__ import annotations

import os

import dramatiq
from dramatiq.brokers.redis import RedisBroker

redis_url = os.getenv(
    "REDIS_URL",
    "redis://localhost:6379/0",
)

broker = RedisBroker(url=redis_url)

dramatiq.set_broker(broker)
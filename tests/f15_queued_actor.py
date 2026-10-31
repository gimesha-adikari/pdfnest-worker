"""Observability-only middleware for the opt-in F-15 queued E2E harness.

The actor and RedisBroker are the real application implementations. No actor,
claim, storage or native operation is replaced.
"""
import json
import os
import sys
import threading

from dramatiq import Middleware
from app.jobs import actors  # Registers the actual production actors.
from app.core.broker import broker
from app.core.redis import redis_client

_current = threading.local()


def event(kind, **values):
    payload = json.dumps({"event": kind, "pid": os.getpid(), **values}) + "\n"
    fd = os.open(os.environ["F15_ACTOR_EVENTS"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)


def status(job_id):
    raw = redis_client.get("pdfnest:jobs:" + job_id)
    return json.loads(raw)["status"] if raw else None


class Observe(Middleware):
    def before_process_message(self, broker, message):
        _current.job_id = message.args[0]
        event("before", job_id=message.args[0], message_id=message.message_id,
              actor=message.actor_name, status=status(message.args[0]))

    def after_process_message(self, broker, message, *, result=None, exception=None):
        event("after", job_id=message.args[0], message_id=message.message_id,
              status=status(message.args[0]), exception=type(exception).__name__ if exception else None)
        _current.job_id = None


def audit(name, args):
    if name == "subprocess.Popen" and "app.core.native_supervisor" in args[1]:
        job_id = getattr(_current, "job_id", None)
        event("native_start", job_id=job_id, status=status(job_id) if job_id else None)


sys.addaudithook(audit)
broker.add_middleware(Observe())

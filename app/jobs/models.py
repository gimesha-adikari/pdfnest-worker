from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobQueue(str, Enum):
    default = "default"
    office = "office"
    render = "render"
    editor = "editor"
    ocr = "ocr"


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    cancel_requested = "cancel_requested"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class JobRecord(BaseModel):
    id: str
    job_type: str
    queue_name: JobQueue = JobQueue.default
    status: JobState = JobState.queued
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: int = 0
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False


class TestJobRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    queue_name: JobQueue = JobQueue.default
    steps: int = Field(default=10, ge=1, le=1000)
    duration_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
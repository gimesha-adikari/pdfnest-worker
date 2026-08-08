from __future__ import annotations

from unittest.mock import MagicMock, patch
import json

from app.jobs.limiter import acquire_lease, release_lease, renew_lease


def test_acquire_lease_success():
    with patch("app.jobs.limiter._acquire_script") as mock_script:
        mock_script.return_value = json.dumps({"status": "ACCEPTED", "reason": "ACQUIRED"})
        acquired, reason = acquire_lease("task-123", "user-1")
        assert acquired is True
        assert reason == "ACQUIRED"


def test_acquire_lease_rejected():
    with patch("app.jobs.limiter._acquire_script") as mock_script:
        mock_script.return_value = json.dumps({
            "status": "REJECTED",
            "reason": "GLOBAL_CAPACITY_EXHAUSTED",
            "active": 4,
            "max": 4,
        })
        acquired, reason = acquire_lease("task-456", "user-1")
        assert acquired is False
        assert reason == "GLOBAL_CAPACITY_EXHAUSTED"


def test_release_lease():
    with patch("app.jobs.limiter._release_script") as mock_script:
        mock_script.return_value = json.dumps({"status": "RELEASED"})
        release_lease("task-123", "user-1")
        assert mock_script.called is True


def test_renew_lease():
    with patch("app.jobs.limiter._renew_script") as mock_script:
        mock_script.return_value = json.dumps({"status": "RENEWED"})
        renewed = renew_lease("task-123", "user-1")
        assert renewed is True

from datetime import timedelta
from types import SimpleNamespace
import fitz
import pytest
from app.jobs.actors import (
    _run_searchable_pdf_job,
    editor_compile_job,
    editor_extract_job,
    is_non_retryable_error,
    markup_highlight_job,
    markup_strikeout_job,
    markup_underline_job,
    ocr_v2_job,
)
from app.jobs.models import JobState
from app.jobs.store import create_job, get_job, prune_expired_job_index, save_job, update_job, utcnow
from app.core.redis import redis_client
from app.core.ocr_v2.errors import RenderingNotEligibleError


def test_actor_time_limits():
    assert editor_extract_job.options.get("time_limit") == 600_000
    assert editor_compile_job.options.get("time_limit") == 600_000
    assert markup_highlight_job.options.get("time_limit") == 900_000
    assert markup_underline_job.options.get("time_limit") == 900_000
    assert markup_strikeout_job.options.get("time_limit") == 900_000


def test_is_non_retryable_error_classification():
    assert is_non_retryable_error(fitz.FileDataError("cannot open broken document")) is True
    assert is_non_retryable_error(fitz.EmptyFileError("0 bytes file")) is True

    # Generic ValueError / RuntimeError / infrastructure errors must NOT be classified as non-retryable
    assert is_non_retryable_error(ValueError("Password required")) is False
    assert is_non_retryable_error(RuntimeError("Invalid state")) is False
    assert is_non_retryable_error(OSError("Disk full")) is False
    assert is_non_retryable_error(ConnectionError("R2 socket closed")) is False


def test_job_index_pruning():
    job = create_job("test_prune")
    assert get_job(job.id) is not None

    # Manually add an expired job ID to index with score 2 days ago
    expired_id = "expired-job-12345"
    old_timestamp = 1000.0  # Unix timestamp in past
    redis_client.zadd("pdfnest:jobs:index", {expired_id: old_timestamp})

    # Prune
    prune_expired_job_index()

    # Verify expired_id was removed from zset
    score = redis_client.zscore("pdfnest:jobs:index", expired_id)
    assert score is None

    # Verify active job was preserved
    active_score = redis_client.zscore("pdfnest:jobs:index", job.id)
    assert active_score is not None


def test_stale_job_detection_on_read():
    job = create_job("test_stale")
    job.status = JobState.running
    job.updated_at = utcnow() - timedelta(minutes=25)
    redis_client.set(f"pdfnest:jobs:{job.id}", job.model_dump_json(), ex=86400)

    read_job = get_job(job.id)
    assert read_job is not None
    assert read_job.status == JobState.failed
    assert "Worker process terminated unexpectedly" in (read_job.error or "")


def test_healthy_long_running_job_not_stale():
    job = create_job("test_healthy_long")
    job.status = JobState.running
    job.updated_at = utcnow() - timedelta(minutes=2)
    save_job(job)

    read_job = get_job(job.id)
    assert read_job is not None
    assert read_job.status == JobState.running


def test_terminal_state_immutability():
    job = create_job("test_terminal")
    update_job(job.id, status=JobState.failed, error="Initial failure")

    # Late worker attempts to mark succeeded
    update_job(job.id, status=JobState.succeeded)

    read_job = get_job(job.id)
    assert read_job is not None
    assert read_job.status == JobState.failed


def test_monotonic_progress():
    job = create_job("test_monotonic")
    update_job(job.id, status=JobState.running, progress=60)

    # Attempt to regress progress to 30
    update_job(job.id, status=JobState.running, progress=30)

    read_job = get_job(job.id)
    assert read_job is not None
    assert read_job.progress == 60


def test_duplicate_ocr_delivery_does_not_delete_active_text_input(monkeypatch):
    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="user:alice", payload={}))
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: None)
    delete = lambda _key: pytest.fail("duplicate delivery must not delete the active input")
    monkeypatch.setattr("app.jobs.actors.delete_object", delete)

    ocr_v2_job("123e4567-e89b-12d3-a456-426614174000", "jobs/ocr_v2/input/document.pdf", "document.pdf", "eng", "AUTO")


def test_duplicate_ocr_delivery_does_not_delete_active_searchable_inputs(monkeypatch):
    monkeypatch.setattr("app.jobs.actors.get_job", lambda _job_id: SimpleNamespace(status=JobState.queued, owner_identity="user:alice", payload={}))
    monkeypatch.setattr("app.jobs.actors.check_cancellation", lambda _job_id: None)
    monkeypatch.setattr("app.jobs.actors.claim_job", lambda _job_id: None)
    delete = lambda _key: pytest.fail("duplicate delivery must not delete the active input")
    monkeypatch.setattr("app.jobs.actors.delete_object", delete)

    _run_searchable_pdf_job(
        "123e4567-e89b-12d3-a456-426614174000",
        "eng",
        [{"source_key": "jobs/ocr_v2/searchable_pdf/input/page.png", "source_name": "page.png"}],
        "page.png",
    )


def test_searchable_renderer_failure_preserves_typed_stage_classification():
    from app.jobs.actors import _searchable_failure_code, _searchable_failure_message

    code = _searchable_failure_code(RenderingNotEligibleError("artifact validation failed"), "PDF_RENDER")
    assert code == "PDF_RENDER_FAILURE"
    assert _searchable_failure_message(code, "PDF_RENDER") == "Searchable PDF V2 job failed during PDF_RENDER (PDF_RENDER_FAILURE)."


def test_searchable_engine_failure_precedes_profile_capability_failure():
    from app.jobs.actors import _raise_primary_page_failure
    from app.core.ocr_v2.errors import EngineUnavailableError

    result = SimpleNamespace(
        pages=(SimpleNamespace(status=SimpleNamespace(value="FAILED"), failure_code="EngineUnavailableError"),)
    )

    with pytest.raises(EngineUnavailableError) as raised:
        _raise_primary_page_failure(result)

    assert "unavailable" in str(raised.value)

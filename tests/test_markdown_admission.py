"""Markdown actor admission against disposable, loopback-only Redis."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.native_operations import convert_pdf_to_markdown
from app.jobs import actors, limiter, store
from app.jobs.models import JobState


@pytest.fixture(scope="module")
def isolated_redis(tmp_path_factory):
    executable = shutil.which("redis-server")
    assert executable, "redis-server is required for the isolated admission tests"
    directory = tmp_path_factory.mktemp("markdown-redis")
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    process = subprocess.Popen(
        [executable, "--bind", "127.0.0.1", "--port", str(port),
         "--save", "", "--appendonly", "no", "--dir", str(directory),
         "--maxmemory", "32mb", "--maxmemory-policy", "noeviction"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = Redis(host="127.0.0.1", port=port, decode_responses=True,
                   socket_connect_timeout=1, socket_timeout=2)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except RedisConnectionError:
                if process.poll() is not None:
                    pytest.fail("isolated Redis exited before startup")
                time.sleep(0.02)
        else:
            pytest.fail("isolated Redis did not start")
        yield client
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def admission(monkeypatch, tmp_path, isolated_redis):
    prefix = f"f14:{uuid4()}:"
    monkeypatch.setattr(store, "redis_client", isolated_redis)
    monkeypatch.setattr(store, "JOB_KEY_PREFIX", prefix + "jobs:")
    monkeypatch.setattr(store, "JOB_INDEX_KEY", prefix + "index")
    monkeypatch.setattr(limiter, "LEASE_KEY_PREFIX", prefix + "lease:")
    monkeypatch.setattr(limiter, "ACTIVE_GLOBAL_KEY", prefix + "active")
    monkeypatch.setattr(limiter, "ACTIVE_IDENTITY_PREFIX", prefix + "identity:")
    monkeypatch.setattr(limiter, "_acquire_script", isolated_redis.register_script(limiter.ATOMIC_ACQUIRE_LUA))
    monkeypatch.setattr(limiter, "_release_script", isolated_redis.register_script(limiter.ATOMIC_RELEASE_LUA))
    monkeypatch.setattr(limiter, "get_global_limit", lambda: 2)
    monkeypatch.setattr(limiter, "get_identity_limit", lambda: 1)
    calls = SimpleNamespace(downloads=[], conversions=[], uploads=[], releases=[], waits=[], paths=[])

    def download(key, path):
        calls.downloads.append(key)
        Path(path).write_bytes(b"synthetic input; native conversion is mocked")

    def convert(**kwargs):
        calls.conversions.append(kwargs)
        kwargs["cancellation_check"]()
        kwargs["progress_cb"](70, "Synthetic conversion")
        return "# Synthetic Markdown\nExpected content.\n"

    def upload(path, key, **kwargs):
        calls.uploads.append((key, Path(path).read_text(), kwargs))

    def temporary(prefix, suffix):
        path = str(tmp_path / f"{prefix}{uuid4()}{suffix}")
        Path(path).touch()
        calls.paths.append(path)
        return path

    def release(job_id, owner):
        calls.releases.append((job_id, owner))
        limiter.release_lease(job_id, owner)

    monkeypatch.setattr(actors, "download_to_path", download)
    monkeypatch.setattr(actors, "convert_pdf_to_markdown", convert)
    monkeypatch.setattr(actors, "upload_path", upload)
    monkeypatch.setattr(actors, "temp_file_path", temporary)
    monkeypatch.setattr(actors, "release_lease", release)
    monkeypatch.setattr(actors, "time", SimpleNamespace(sleep=calls.waits.append))
    job = store.create_job("pdf_to_markdown", owner_identity="user:synthetic")
    task_key = f"pdfnest:tasks:{job.id}"
    isolated_redis.set(task_key, json.dumps({
        "ownerIdentity": "user:synthetic", "reservationId": "synthetic-reservation",
        "downloadToken": "synthetic-download-token",
    }))
    calls.job = job
    calls.redis = isolated_redis
    calls.run = lambda: actors.pdf_to_markdown_job(job.id, "synthetic/input.pdf", source_name="example.pdf")
    yield calls
    assert all(not Path(path).exists() for path in calls.paths)
    isolated_redis.delete(task_key)
    keys = list(isolated_redis.scan_iter(prefix + "*"))
    if keys:
        isolated_redis.delete(*keys)


@pytest.mark.parametrize("reason", ["GLOBAL_CAPACITY_EXHAUSTED", "IDENTITY_CAPACITY_EXHAUSTED", "REDIS_ERROR"])
def test_denied_lease_never_processes_and_exhausts_bounded_retries(admission, monkeypatch, reason):
    attempts = []

    def deny(*args):
        attempts.append(args)
        return False, reason

    monkeypatch.setattr(actors, "acquire_lease", deny)
    admission.run()
    assert not admission.downloads
    assert not admission.conversions
    assert not admission.uploads
    assert not admission.paths
    assert not admission.releases
    assert len(attempts) == 4
    assert admission.waits == [5, 5, 5]
    job = store.get_job(admission.job.id)
    assert job.status == JobState.failed
    assert job.finished_at is not None
    assert job.result is None
    assert job.error_code == ("TASK_STORAGE_UNAVAILABLE" if reason == "REDIS_ERROR" else "ENGINE_UNAVAILABLE")
    assert json.loads(admission.redis.get(f"pdfnest:tasks:{job.id}"))["status"] == "FAILED"
    admission.run()  # A broker redelivery cannot reset the admission budget.
    assert len(attempts) == 4


def test_success_preserves_result_and_task_ownership(admission):
    admission.run()
    job = store.get_job(admission.job.id)
    assert job.status == JobState.succeeded
    assert job.progress == 100
    assert job.result["artifact_name"] == "example.md"
    assert admission.uploads == [(job.result["artifact_key"], "# Synthetic Markdown\nExpected content.\n",
                                  {"content_type": "text/markdown; charset=utf-8"})]
    assert admission.releases == [(job.id, "user:synthetic")]
    assert not admission.redis.exists(limiter.LEASE_KEY_PREFIX + job.id)
    mirror = json.loads(admission.redis.get(f"pdfnest:tasks:{job.id}"))
    assert mirror["status"] == "COMPLETED"
    assert mirror["ownerIdentity"] == "user:synthetic"
    assert mirror["reservationId"] == "synthetic-reservation"
    assert mirror["downloadToken"] == "synthetic-download-token"
    admission.run()
    assert len(admission.conversions) == len(admission.releases) == 1


def test_admitted_synthetic_pdf_completes_native_markdown_conversion(admission, monkeypatch):
    import pymupdf as fitz

    def download(_key, path):
        with fitz.open() as document:
            page = document.new_page(width=300, height=200)
            page.insert_text((25, 45), "Synthetic admission success.")
            document.save(path)

    monkeypatch.setattr(actors, "download_to_path", download)
    monkeypatch.setattr(actors, "convert_pdf_to_markdown", convert_pdf_to_markdown)
    admission.run()
    assert store.get_job(admission.job.id).status == JobState.succeeded
    assert "Synthetic admission success." in admission.uploads[0][1]
    assert len(admission.releases) == 1


@pytest.mark.parametrize("same_owner", [False, True])
def test_real_capacity_denial_preserves_other_lease(admission, monkeypatch, same_owner):
    monkeypatch.setattr(limiter, "get_global_limit", lambda: 1 if not same_owner else 2)
    owner = "user:synthetic" if same_owner else "user:other"
    assert limiter.acquire_lease("other-job", owner)[0]
    admission.run()
    assert not admission.downloads
    assert not admission.releases
    assert store.get_job(admission.job.id).status == JobState.failed
    assert admission.redis.exists(limiter.LEASE_KEY_PREFIX + "other-job")


def test_capacity_recovery_within_budget(admission, monkeypatch):
    attempts = []

    def acquire(job_id, owner):
        attempts.append(job_id)
        if len(attempts) < 3:
            return False, "GLOBAL_CAPACITY_EXHAUSTED"
        return limiter.acquire_lease(job_id, owner)

    monkeypatch.setattr(actors, "acquire_lease", acquire)
    admission.run()
    assert len(attempts) == 3
    assert admission.waits == [5, 5]
    assert store.get_job(admission.job.id).status == JobState.succeeded


def test_redis_lease_error_is_safe_and_fail_closed(admission, monkeypatch):
    def unavailable(**kwargs):
        raise RedisConnectionError("synthetic internal /private/storage/socket")

    monkeypatch.setattr(limiter, "_acquire_script", unavailable)
    admission.run()
    job = store.get_job(admission.job.id)
    assert job.status == JobState.failed
    assert job.error_code == "TASK_STORAGE_UNAVAILABLE"
    assert "/private" not in job.model_dump_json()
    assert not admission.downloads
    assert not admission.releases


@pytest.mark.parametrize("stage", ["before", "waiting", "granted", "processing", "exhaustion"])
def test_cancellation_stops_work_and_releases_only_admitted_lease(admission, monkeypatch, stage):
    if stage == "before":
        store.request_cancel(admission.job.id)
    elif stage == "waiting":
        monkeypatch.setattr(actors, "acquire_lease", lambda *_: (False, "GLOBAL_CAPACITY_EXHAUSTED"))
        monkeypatch.setattr(actors, "time", SimpleNamespace(sleep=lambda _: store.request_cancel(admission.job.id)))
    elif stage == "granted":
        def acquire(job_id, owner):
            result = limiter.acquire_lease(job_id, owner)
            store.request_cancel(job_id)
            return result
        monkeypatch.setattr(actors, "acquire_lease", acquire)
    elif stage == "processing":
        def convert(**kwargs):
            store.request_cancel(admission.job.id)
            kwargs["cancellation_check"]()
            pytest.fail("cancelled conversion continued")
        monkeypatch.setattr(actors, "convert_pdf_to_markdown", convert)
    else:
        monkeypatch.setattr(actors, "acquire_lease", lambda *_: (False, "GLOBAL_CAPACITY_EXHAUSTED"))
        update = actors.update_job
        def cancel_before_terminal(job_id, **fields):
            if fields.get("status") == JobState.failed:
                store.request_cancel(job_id)
            return update(job_id, **fields)
        monkeypatch.setattr(actors, "update_job", cancel_before_terminal)
    admission.run()
    assert store.get_job(admission.job.id).status == JobState.cancelled
    assert not admission.uploads
    if stage != "processing":
        assert not admission.downloads
    assert len(admission.releases) == (1 if stage in ("granted", "processing") else 0)


def test_duplicate_delivery_cannot_claim_or_release_active_job(admission, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    original = actors.convert_pdf_to_markdown

    def convert(**kwargs):
        entered.set()
        assert finish.wait(timeout=5)
        return original(**kwargs)

    monkeypatch.setattr(actors, "convert_pdf_to_markdown", convert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(admission.run)
        try:
            assert entered.wait(timeout=5)
            duplicate = pool.submit(admission.run)
            duplicate.result(timeout=3)
            assert not admission.releases
            assert admission.redis.exists(limiter.LEASE_KEY_PREFIX + admission.job.id)
            assert len(admission.downloads) == 1
        finally:
            finish.set()
        first.result(timeout=5)
    assert len(admission.conversions) == len(admission.releases) == 1
    assert store.get_job(admission.job.id).status == JobState.succeeded


def test_simultaneous_queued_deliveries_have_one_claim(admission, monkeypatch):
    barrier = threading.Barrier(2)
    get_job = actors.get_job

    def queued_snapshot(job_id):
        job = get_job(job_id)
        barrier.wait(timeout=5)
        return job

    monkeypatch.setattr(actors, "get_job", queued_snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deliveries = [pool.submit(admission.run) for _ in range(2)]
        for delivery in deliveries:
            delivery.result(timeout=5)
    assert len(admission.downloads) == len(admission.conversions) == len(admission.releases) == 1
    assert store.get_job(admission.job.id).status == JobState.succeeded


def test_existing_task_lease_still_requires_exclusive_job_claim(admission):
    assert limiter.acquire_lease(admission.job.id, "user:synthetic")[0]
    admission.run()
    assert store.get_job(admission.job.id).status == JobState.succeeded
    assert len(admission.releases) == 1


@pytest.mark.parametrize("stage", ["download", "temporary", "running_update", "conversion"])
def test_admitted_failure_releases_lease_and_hides_internal_details(admission, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise OSError("synthetic internal /private/input.pdf")

    if stage == "running_update":
        update = actors.update_job
        def fail_running(job_id, **fields):
            if fields.get("progress") == 5:
                fail()
            return update(job_id, **fields)
        monkeypatch.setattr(actors, "update_job", fail_running)
    else:
        monkeypatch.setattr(actors, {"download": "download_to_path", "temporary": "temp_file_path",
                                    "conversion": "convert_pdf_to_markdown"}[stage], fail)
    with pytest.raises(OSError):
        admission.run()
    job = store.get_job(admission.job.id)
    assert job.status == JobState.failed
    assert "/private" not in job.model_dump_json()
    assert len(admission.releases) == 1
    assert not admission.redis.exists(limiter.LEASE_KEY_PREFIX + job.id)
    assert not admission.uploads
    admission.run()
    assert len(admission.releases) == 1


def test_unavailable_job_store_does_not_acknowledge_a_terminal_write(admission, monkeypatch):
    monkeypatch.setattr(actors, "acquire_lease", lambda *_: (False, "REDIS_ERROR"))
    def fail_update(*args, **kwargs):
        raise RedisConnectionError("synthetic unavailable job store")
    monkeypatch.setattr(actors, "update_job", fail_update)
    with pytest.raises(RedisConnectionError):
        admission.run()
    assert not admission.downloads
    assert not admission.releases
    assert store.get_job(admission.job.id).status == JobState.running
    # Recovery/redelivery must not restart the unadmitted document. The
    # existing stale-job reader supplies terminal recovery after a total
    # write outage; an actor cannot persist a terminal state during it.
    monkeypatch.setattr(actors, "update_job", store.update_job)
    admission.run()
    assert not admission.downloads
    job = store.get_job(admission.job.id)
    job.updated_at = store.utcnow() - timedelta(seconds=store.settings.stuck_job_timeout_seconds + 1)
    admission.redis.set(store.job_key(job.id), job.model_dump_json())
    assert store.get_job(job.id).status == JobState.failed
    assert json.loads(admission.redis.get(f"pdfnest:tasks:{job.id}"))["status"] == "FAILED"


def test_claim_storage_failure_never_acquires_lease(admission, monkeypatch):
    def fail_claim(*args):
        raise RedisConnectionError("synthetic internal /private/redis")
    monkeypatch.setattr(actors, "claim_job", fail_claim)
    monkeypatch.setattr(actors, "acquire_lease", lambda *_: pytest.fail("unclaimed execution acquired a lease"))
    with pytest.raises(RedisConnectionError):
        admission.run()
    assert not admission.downloads
    assert not admission.releases
    assert store.get_job(admission.job.id).status == JobState.queued

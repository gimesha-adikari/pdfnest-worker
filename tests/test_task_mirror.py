import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

from app.jobs import store
from app.jobs.models import JobState


def test_late_progress_mirror_cannot_overwrite_completed_download(monkeypatch):
    job = store.create_job("mirror-race", owner_identity="user:mirror-test")
    key = f"pdfnest:tasks:{job.id}"
    task = json.loads(store.redis_client.get(key))
    task.update(downloadToken="test-capability", reservationId="test-reservation")
    store.redis_client.set(key, json.dumps(task))
    paused, resume = Event(), Event()

    def pause_late_writer():
        if current_thread().name.startswith("late-progress"):
            paused.set()
            assert resume.wait(5), "completion must release the progress writer"

    monkeypatch.setattr(store, "prune_expired_job_index", pause_late_writer)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-progress") as pool:
            late = pool.submit(store.update_job, job.id, status=JobState.running, progress=25)
            try:
                assert paused.wait(5), "progress must reach the mirror boundary"
                store.update_job(job.id, status=JobState.succeeded, progress=100,
                                 result={"artifact_key": "jobs/mirror-test/output.pdf"})
            finally:
                resume.set()
            late.result(timeout=5)
        assert store.get_job(job.id).status == JobState.succeeded
        mirrored = json.loads(store.redis_client.get(key))
        assert mirrored["status"] == "COMPLETED"
        assert mirrored["progress"] == 100
        assert mirrored["resultKey"] == "jobs/mirror-test/output.pdf"
        assert mirrored["downloadToken"] == "test-capability"
        assert mirrored["reservationId"] == "test-reservation"
        assert mirrored["ownerIdentity"] == "user:mirror-test"
    finally:
        store.redis_client.delete(key, store.job_key(job.id))
        store.redis_client.zrem(store.JOB_INDEX_KEY, job.id)

"""Opt-in real Go -> HTTP -> Redis/Dramatiq -> actor -> artifact qualification.

Run with this Worker's Python environment; see docs/F15_REQUEST_BODY_INTEGRITY.md.
No mocks, default-CI live-service dependency, application patch or hosted access.
"""
from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import ProxyHandler, build_opener

import fitz
import psutil
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from dramatiq import Message
from dramatiq.brokers.redis import RedisBroker

WORKER = Path(__file__).resolve().parents[1]
BACKEND_SHA = "40dfe66886e7c7c5bc3f522e4ca5fcd55e669a02"


def until(probe, *, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(.03)  # Bounded condition polling, never a sleep-only assertion.
    raise AssertionError("fixture condition deadline exceeded")


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process):
    descendants = psutil.Process(process.pid).children(recursive=True) if process.poll() is None else []
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    _, alive = psutil.wait_procs(descendants, timeout=3)
    for child in alive:
        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()
    _, alive = psutil.wait_procs(alive, timeout=3)
    assert all(not p.is_running() or p.status() == psutil.STATUS_ZOMBIE for p in alive)
    return process.returncode


def qualify(backend, redis_binary, report):
    assert subprocess.check_output(["git", "-C", str(backend), "rev-parse", "HEAD"], text=True).strip() == BACKEND_SHA
    subprocess.run(["git", "-C", str(backend), "diff", "--exit-code", "HEAD", "--", "internal", "go.mod", "go.sum"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(WORKER), "diff", "--exit-code", "HEAD", "--", "app", "gateway_protocol"], check=True, stdout=subprocess.DEVNULL)
    report["worker_sha"] = subprocess.check_output(["git", "-C", str(WORKER), "rev-parse", "HEAD"], text=True).strip()
    report["backend_sha"] = BACKEND_SHA
    report["cases"] = []
    report["process_exits"] = {}
    processes, handles, jobs = [], [], []
    with tempfile.TemporaryDirectory(prefix="platen-f15-queue-") as owned:
        root = Path(owned)
        for name in ("objects", "scratch", "redis", "metrics"):
            (root / name).mkdir(mode=0o700)
        redis_port, http_port, metrics_port = port(), port(), port()
        assert len({redis_port, http_port, metrics_port}) == 3
        environment = {
            "PATH": os.environ["PATH"], "LANG": "C.UTF-8", "APP_ENV": "test",
            "PYTHONPATH": str(WORKER), "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "STORAGE_MODE": "local",
            "LOCAL_STORAGE_DIR": str(root / "objects"), "TMPDIR": str(root / "scratch"),
            "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            "WORKER_SHARED_SECRET": "synthetic-f15-" + uuid.uuid4().hex,
            "PDFNEST_WORKER_URL": f"http://127.0.0.1:{http_port}",
            "FILE_ENCRYPTION_KEY": "", "ENABLE_PERSISTENT_RENDER_POOL": "false",
            "ACTOR_HEARTBEAT_REQUIRED": "false", "GLOBAL_HEAVY_EXECUTION_LIMIT": "1",
            "PER_IDENTITY_HEAVY_EXECUTION_LIMIT": "1", "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1", "PDF_TO_MARKDOWN_ENGINE": "internal",
            "F15_ACTOR_EVENTS": str(root / "actor-events.jsonl"),
            "dramatiq_prom_host": "127.0.0.1", "dramatiq_prom_port": str(metrics_port),
            "dramatiq_prom_db": str(root / "metrics"),
        }
        report["infrastructure"] = {"redis": f"127.0.0.1:{redis_port}/0", "http": f"127.0.0.1:{http_port}", "actor_processes": 1, "actor_threads": 1, "storage": "disposable filesystem", "redis_maxmemory": "32mb"}

        def launch(name, argv):
            handle = open(root / (name + ".log"), "w+")
            handles.append(handle)
            proc = subprocess.Popen(argv, cwd=WORKER, env=environment, start_new_session=True, stdout=handle, stderr=subprocess.STDOUT)
            processes.append((name, proc))
            report.setdefault("processes", []).append({"name": name, "pid": proc.pid, "command": argv})
            return proc

        r = None
        try:
            launch("redis", [str(redis_binary), "--bind", "127.0.0.1", "--protected-mode", "yes", "--port", str(redis_port), "--save", "", "--appendonly", "no", "--dir", str(root / "redis"), "--maxmemory", "32mb", "--maxmemory-policy", "noeviction"])
            r = Redis(host="127.0.0.1", port=redis_port, db=0, socket_timeout=2, socket_connect_timeout=1)
            def ready_redis():
                try: return r.ping()
                except RedisConnectionError: return False
            until(ready_redis)
            assert r.dbsize() == 0
            api = launch("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(http_port), "--log-level", "warning", "--no-access-log"])
            opener = build_opener(ProxyHandler({}))
            def ready_http():
                assert api.poll() is None, "API exited during startup"
                try:
                    with opener.open(environment["PDFNEST_WORKER_URL"] + "/health/live", timeout=1) as response:
                        return response.status == 200
                except OSError: return False
            until(ready_http)
            with tempfile.TemporaryDirectory(prefix="f15-queued-go-", dir=backend) as go_source:
                source = Path(go_source) / "main.go"
                shutil.copyfile(WORKER / "tests/f15_queued_sender.go", source)
                build_env = {**environment, "GOPATH": subprocess.check_output(["go", "env", "GOPATH"], text=True).strip(), "GOCACHE": str(root / "go-cache"), "GOPROXY": "off", "GOSUMDB": "off"}
                # An explicitly supplied development cache is optional; default cache is test-owned.
                if os.getenv("F15_GO_CACHE"): build_env["GOCACHE"] = os.environ["F15_GO_CACHE"]
                subprocess.run(["go", "build", "-o", str(root / "sender"), str(source)], cwd=backend, env=build_env, check=True, timeout=180, stdout=subprocess.DEVNULL)
            owner = "user:f15-synthetic-" + uuid.uuid4().hex
            def go(action, *, allow_error=False, **values):
                command = {"action": action, "owner": owner, "request_id": "f15-" + uuid.uuid4().hex, **values}
                result = subprocess.run([str(root / "sender")], input=json.dumps(command), text=True, capture_output=True, env=environment, cwd=root, timeout=20)
                value = json.loads(result.stdout)
                if not allow_error: assert result.returncode == 0, value
                return value
            def indexed_jobs(): return set(r.zrange("pdfnest:jobs:index", 0, -1))
            def queue(): return r.lrange("dramatiq:ocr", 0, -1)
            source_key = "jobs/ocr_v2/input/f15-synthetic.pdf"
            path = root / "objects" / source_key
            path.parent.mkdir(parents=True)
            with fitz.open() as pdf:
                for text in ("PLATEN F15 QUEUED ALPHA", "PLATEN F15 QUEUED BETA"):
                    page = pdf.new_page(width=300, height=150)
                    page.insert_text((20, 45), text, fontsize=12)
                pdf.save(path)
            report["input_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            for variant in ("tampered", "missing_hash", "malformed_hash", "invalid_hmac", "hashless"):
                assert go("raw", variant=variant, source=source_key)["statuses"] == [401]
                assert not indexed_jobs() and not queue() and r.hlen("dramatiq:ocr.msgs") == 0
                report["cases"].append({"case": variant, "status": 401, "jobs": 0, "messages": 0})
            submission = go("submit", source=source_key)
            job_id = submission["job_id"]; jobs.append(job_id)
            assert submission["status"] == "queued" and submission["owner_identity"] == owner
            record = json.loads(r.get("pdfnest:jobs:" + job_id))
            assert record["payload"]["source_key"] == source_key
            assert len(queue()) == 1 and len(indexed_jobs()) == 1
            queued = json.loads(r.hget("dramatiq:ocr.msgs", queue()[0]))
            assert queued["actor_name"] == "ocr_v2_job" and queued["args"][0] == job_id
            producer = RedisBroker(url=environment["REDIS_URL"])
            try:
                duplicate = producer.enqueue(Message(**queued).copy(message_id=str(uuid.uuid4())))
            finally:
                producer.close()
            assert len(queue()) == 2
            assert go("raw", variant="replay", job_id=job_id)["statuses"] == [200, 401]
            assert len(indexed_jobs()) == 1 and len(queue()) == 2
            report["cases"].append({"case": "nonce_replay", "statuses": [200, 401], "jobs": 1, "messages": 2})
            actor = launch("actor", [sys.executable, "-m", "dramatiq", "tests.f15_queued_actor:broker", "--processes", "1", "--threads", "1", "--queues", "ocr", "--worker-shutdown-timeout", "5000"])
            def events():
                p = root / "actor-events.jsonl"
                return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
            def completed(jid):
                assert actor.poll() is None, "actor exited unexpectedly"
                value = go("status", job_id=jid)
                return value if value["status"] in {"succeeded", "failed", "cancelled"} else None
            status = until(lambda: completed(job_id))
            assert status["status"] == "succeeded", status
            until(lambda: len([e for e in events() if e["event"] == "after" and e["job_id"] == job_id]) == 2)
            deliveries = [e for e in events() if e.get("job_id") == job_id]
            before = [e for e in deliveries if e["event"] == "before"]
            assert len(before) == 2 and before[0]["status"] == "queued" and before[1]["status"] == "succeeded"
            assert all(e["pid"] != os.getpid() and e["pid"] != api.pid for e in before)
            native = [e for e in deliveries if e["event"] == "native_start"]
            assert len(native) == 1 and native[0]["status"] == "running", deliveries
            result = go("result", job_id=job_id)
            assert all(text in result["markdown"] for text in ("PLATEN F15 QUEUED ALPHA", "PLATEN F15 QUEUED BETA"))
            assert len(result["pages"]) == 2
            record = json.loads(r.get("pdfnest:jobs:" + job_id))
            artifact = root / "objects" / record["result"]["artifact_key"]
            assert artifact.is_file() and json.loads(artifact.read_text()) == result
            result_bytes = artifact.read_bytes()
            assert go("raw", variant="unsigned_result", job_id=job_id)["statuses"] == [401]
            report["cases"].append({"case": "unsigned_result", "status": 401})
            assert not path.exists()
            report["positive"] = {"job_id": job_id, "owner_identity": owner, "source_key": source_key, "states": [submission["status"], native[0]["status"], status["status"]], "started_at": status["started_at"], "finished_at": status["finished_at"], "artifact_key": record["result"]["artifact_key"], "artifact_sha256": hashlib.sha256(result_bytes).hexdigest(), "artifact_size": len(result_bytes), "pages": len(result["pages"]), "deliveries": before, "native_starts": len(native), "native_events": native, "duplicate_message_id": duplicate.message_id}
            assert status["started_at"] and status["finished_at"]
            for case in ("missing_source", "corrupt_pdf"):
                key = "jobs/ocr_v2/input/" + case + ".pdf"
                corrupt = root / "objects" / key
                if case == "corrupt_pdf": corrupt.write_bytes(b"%PDF-1.7\nSynthetic invalid PDF, no objects.\n")
                failed = go("submit", source=key); failed_id = failed["job_id"]; jobs.append(failed_id)
                failed = until(lambda: completed(failed_id))
                assert failed["status"] == "failed" and not failed.get("result_key"), failed
                assert str(root) not in failed["error"] and key not in failed["error"]
                retrieval = go("result", job_id=failed_id, allow_error=True)
                assert "error" in retrieval
                until(lambda: any(e["event"] == "after" and e["job_id"] == failed_id for e in events()))
                assert not corrupt.exists()
                report["cases"].append({"case": case, "job_id": failed_id, "state": failed["status"], "error_code": failed.get("error_code"), "public_error": failed["error"], "result_rejected": True})
            until(lambda: not queue() and r.hlen("dramatiq:ocr.msgs") == 0)
            assert len(indexed_jobs()) == 3
            assert list((root / "objects").rglob("*.json")) == [artifact]
            # Native admission deliberately retains a fixed empty flock inode.
            # Verify it is the only coordination file and its lock is released.
            slot = root / "scratch" / f"platen-native-slots-{os.getuid()}" / "0"
            assert slot.is_file() and slot.stat().st_size == 0
            with slot.open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            scratch_files = [p for p in (root / "scratch").rglob("*") if p.is_file()]
            assert scratch_files == [slot], scratch_files
            observed_processes = [psutil.Process(api.pid), psutil.Process(actor.pid), *psutil.Process(actor.pid).children(recursive=True)]
            for process in observed_processes:
                for fd in Path(f"/proc/{process.pid}/fd").iterdir():
                    try: target = os.readlink(fd)
                    except FileNotFoundError: continue
                    assert not (str(root / "scratch") in target and "(deleted)" in target), "unlinked auth spool leaked"
            assert r.zcard("pdfnest:limiter:active") == 0
            assert not list(r.scan_iter("pdfnest:limiter:lease:*"))
            report["cleanup_checks"] = {"queued_messages": 0, "broker_message_payloads": 0, "jobs": 3, "artifacts": 1, "active_leases": 0, "processing_or_auth_files": 0, "released_fixed_capacity_lock_files": 1}
            report["cancellation"] = "Not exercised: no deterministic externally signalled in-flight native checkpoint; no timing-based cancellation claim."
            report["result"] = "LOCAL_QUEUED_LIFECYCLE_PASS"
        except BaseException:
            report["result"] = "FAILED"
            report["remaining_scratch"] = [str(p.relative_to(root)) for p in (root / "scratch").rglob("*") if p.is_file()]
            report["log_tails"] = {p.stem: p.read_text(errors="replace")[-10000:] for p in root.glob("*.log")}
            raise
        finally:
            if r is not None: r.close()
            cleanup_errors = []
            for name, process in reversed(processes):
                try: report["process_exits"][name] = stop(process)
                except BaseException as error: cleanup_errors.append(f"{name}: {type(error).__name__}")
            for handle in handles: handle.close()
            # The existing result route uses exact job-owned /tmp readback paths.
            for job_id in jobs:
                for suffix in (".json", ".pdf"):
                    target = Path("/tmp") / ("pdfnest-ocr-v2-result-" + job_id + suffix)
                    assert not target.exists(), "result readback file leaked"
            assert not cleanup_errors, cleanup_errors
    report["temporary_root_removed"] = not root.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--redis-server", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {}
    try:
        qualify(args.backend_root.resolve(), args.redis_server.absolute(), report)
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k:v for k,v in report.items() if k != "log_tails"}, indent=2))


if __name__ == "__main__": main()

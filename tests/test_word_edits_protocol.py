from __future__ import annotations

from copy import deepcopy
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.word_edits import (
    WORD_EDIT_PROTOCOL,
    MAX_WORD_REPLACEMENT_SCALARS,
    WordEditProtocolError,
    baseline_digest,
    build_word_edit_baseline,
    native_transport_digest,
    validate_word_edit_request,
    validate_word_edit_transport as transport_operation,
)
from app.api.tools.editor import router as editor_router
from app.core import native_operations
from app.jobs import actors
from app.jobs.models import JobQueue, JobState


def extraction_fixture():
    result = {
        "schema_version": "ocr_v2_editor_layout.v1",
        "ocr_v2": True,
        "language_mode": "EXPLICIT",
        "languages": ["eng"],
        "pages": [{
            "page_num": 1,
            "elements": [{
                "id": "line-1",
                "word_ids": ["word-1", "word-2", "word-3"],
                "word_geometry": [
                    {"id": "word-1", "text": "again", "x": 1, "y": 2, "width": 3, "height": 4},
                    {"id": "word-2", "text": "and", "x": 4, "y": 2, "width": 2, "height": 4},
                    {"id": "word-3", "text": "again", "x": 7, "y": 2, "width": 3, "height": 4},
                ],
            }],
        }],
    }
    source_sha = "a" * 64
    engine_fingerprint = "sha256:" + "b" * 64
    result["word_edit_protocols"] = [WORD_EDIT_PROTOCOL]
    result["word_edit_baseline"] = build_word_edit_baseline(
        "extract-1", source_sha, engine_fingerprint, result
    )
    return {
        "id": "extract-1",
        "job_type": "editor_extract",
        "status": "succeeded",
        "payload": {
            "source_key": "owned/user/editor_source/original.pdf",
            "source_name": "original.pdf",
            "consumer": "general_editor",
            "ocr_v2": True,
        },
        "result": result,
    }


def request_fixture(extraction):
    return {
        "edit_protocol": WORD_EDIT_PROTOCOL,
        "source_tracker": extraction["payload"]["source_key"],
        "extraction_job_id": extraction["id"],
        "baseline_digest": extraction["result"]["word_edit_baseline"]["digest"],
        "edits": [
            {
                "page_num": 1,
                "element_id": "line-1",
                "word_id": "word-3",
                "expected_original_text": "again",
                "replacement_text": "🧪𠮷",
                "style_override": None,
                "fit_policy": "preserve_neighbors",
            },
            {
                "page_num": 1,
                "element_id": "line-1",
                "word_id": "word-2",
                "expected_original_text": "and",
                "replacement_text": "",
                "style_override": None,
                "fit_policy": "preserve_neighbors",
            },
        ],
    }


def test_validates_final_snapshot_against_immutable_baseline_and_preserves_unicode():
    extraction = extraction_fixture()
    request = request_fixture(extraction)
    validated = validate_word_edit_request(request, extraction)
    assert validated == request
    assert validated["edits"][0]["word_id"] == "word-3"
    assert validated["edits"][0]["replacement_text"] == "🧪𠮷"
    assert validated["edits"][1]["replacement_text"] == ""
    assert native_transport_digest(validated) == native_transport_digest(deepcopy(request))


@pytest.mark.parametrize(
    ("mutate_request", "mutate_extraction"),
    [
        (lambda r: r.update(edit_protocol="platen_word_edits.v9"), lambda e: None),
        (lambda r: r.update(source_tracker="owned/other/editor_source/a.pdf"), lambda e: None),
        (lambda r: r["edits"][0].update(page_num=2), lambda e: None),
        (lambda r: r["edits"][0].update(element_id="line-other"), lambda e: None),
        (lambda r: r["edits"][0].update(word_id="word-missing"), lambda e: None),
        (lambda r: r["edits"][0].update(expected_original_text="stale"), lambda e: None),
        (lambda r: r["edits"].append(deepcopy(r["edits"][0])), lambda e: None),
        (lambda r: r, lambda e: e.update(status="queued")),
        (lambda r: r, lambda e: e.update(job_type="editor_compile")),
        (lambda r: r, lambda e: e["payload"].update(consumer="studio")),
        (lambda r: r, lambda e: e["result"]["pages"][0]["elements"][0]["word_geometry"][2].update(text="changed")),
    ],
)
def test_rejects_untrusted_or_stale_word_edit_targets(mutate_request, mutate_extraction):
    extraction = extraction_fixture()
    request = request_fixture(extraction)
    mutate_request(request)
    mutate_extraction(extraction)
    with pytest.raises(WordEditProtocolError):
        validate_word_edit_request(request, extraction)


def test_rejects_ambiguous_target_and_conflicting_duplicate_operations():
    extraction = extraction_fixture()
    duplicate = deepcopy(extraction["result"]["pages"][0]["elements"][0]["word_geometry"][0])
    extraction["result"]["pages"][0]["elements"][0]["word_geometry"].append(duplicate)
    extraction["result"]["pages"][0]["elements"][0]["word_ids"].append(duplicate["id"])
    request = request_fixture(extraction)
    request["edits"] = [request["edits"][0] | {"word_id": "word-1"}]
    extraction["result"]["word_edit_baseline"] = build_word_edit_baseline(
        extraction["id"], "a" * 64, "sha256:" + "b" * 64, extraction["result"]
    )
    request["baseline_digest"] = extraction["result"]["word_edit_baseline"]["digest"]
    with pytest.raises(WordEditProtocolError, match="ambiguous"):
        validate_word_edit_request(request, extraction)


def test_rejects_bad_baseline_digest_and_unknown_or_extra_wire_fields():
    extraction = extraction_fixture()
    request = request_fixture(extraction)
    request["baseline_digest"] = "sha256:" + "c" * 64
    with pytest.raises(WordEditProtocolError, match="digest"):
        validate_word_edit_request(request, extraction)

    request = request_fixture(extraction)
    request["unexpected"] = True
    with pytest.raises(WordEditProtocolError, match="fields"):
        validate_word_edit_request(request, extraction)


def test_rejects_oversized_replacement_and_more_than_protocol_operation_cap():
    extraction = extraction_fixture()
    request = request_fixture(extraction)
    request["edits"][0]["replacement_text"] = "x" * 4097
    with pytest.raises(WordEditProtocolError, match="4096"):
        validate_word_edit_request(request, extraction)


def test_rejects_payload_that_would_exceed_the_fixed_native_ipc_frame():
    extraction = extraction_fixture()
    words = extraction["result"]["pages"][0]["elements"][0]["word_geometry"]
    words.clear()
    word_ids = extraction["result"]["pages"][0]["elements"][0]["word_ids"]
    word_ids.clear()
    edits = []
    for index in range(1000):
        word_id = f"word-{index}"
        words.append({"id": word_id, "text": f"original-{index}"})
        word_ids.append(word_id)
        edits.append({
            "page_num": 1,
            "element_id": "line-1",
            "word_id": word_id,
            "expected_original_text": f"original-{index}",
            "replacement_text": "🧪" * MAX_WORD_REPLACEMENT_SCALARS,
            "style_override": None,
            "fit_policy": "preserve_neighbors",
        })
    extraction["result"]["word_edit_baseline"] = build_word_edit_baseline(
        extraction["id"], "a" * 64, "sha256:" + "b" * 64, extraction["result"]
    )
    request = request_fixture(extraction)
    request["baseline_digest"] = extraction["result"]["word_edit_baseline"]["digest"]
    request["edits"] = edits
    with pytest.raises(WordEditProtocolError, match="frame limit"):
        validate_word_edit_request(request, extraction)


def test_worker_api_queues_exact_v1_envelope_and_rejects_unknown_versions(monkeypatch):
    extraction = extraction_fixture()
    request = request_fixture(extraction)
    queued = []
    monkeypatch.setattr(editor_router, "get_job", lambda _job_id: SimpleNamespace(**extraction, owner_identity="owner"))
    monkeypatch.setattr(
        editor_router,
        "create_job",
        lambda job_type, **kwargs: SimpleNamespace(id="word-job", status=JobState.queued, queue_name=JobQueue.editor),
    )
    monkeypatch.setattr(editor_router, "editor_word_edit_compile_job", SimpleNamespace(send=lambda *args: queued.append(args)))
    payload = editor_router.WordEditCompileRequest.model_validate(request)
    result = asyncio.run(editor_router.compile_word_edits(payload))
    assert result.job_id == "word-job"
    assert queued == [("word-job", request)]

    with pytest.raises(ValidationError):
        editor_router.WordEditCompileRequest.model_validate(request | {"edit_protocol": "platen_word_edits.v99"})
    with pytest.raises(ValidationError, match="dedicated versioned protocol"):
        editor_router.CompileRequest.model_validate({
            "source_key": request["source_tracker"],
            "pages_json_key": "legacy-layout.json",
            "edit_protocol": WORD_EDIT_PROTOCOL,
            "edits": request["edits"],
        })


def test_operations_cross_native_isolation_with_exact_identity_and_unicode(tmp_path):
    source_bytes = b"native isolation source identity"
    extraction = extraction_fixture()
    extraction["result"]["word_edit_baseline"] = build_word_edit_baseline(
        extraction["id"], hashlib.sha256(source_bytes).hexdigest(), "sha256:" + "b" * 64, extraction["result"]
    )
    envelope = request_fixture(extraction)
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(source_bytes)
    receipt = native_operations.validate_word_edit_transport(input_path=str(source_path), envelope=envelope)
    assert receipt == {
        "protocol": WORD_EDIT_PROTOCOL,
        "extraction_job_id": "extract-1",
        "baseline_digest": envelope["baseline_digest"],
        "operation_count": 2,
        "transport_digest": native_transport_digest(envelope),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


def test_worker_actor_forwards_exact_validated_request_then_fails_without_artifact(monkeypatch, tmp_path):
    source_bytes = b"test source PDF identity"
    extraction = extraction_fixture()
    extraction["result"]["word_edit_baseline"] = build_word_edit_baseline(
        extraction["id"], hashlib.sha256(source_bytes).hexdigest(), "sha256:" + "b" * 64, extraction["result"]
    )
    envelope = request_fixture(extraction)
    extraction_job = SimpleNamespace(**extraction, owner_identity="owner")
    compile_job = SimpleNamespace(
        id="word-job", payload={"ownerIdentity": "owner"}, owner_identity="owner"
    )
    calls = []
    updates = []

    monkeypatch.setattr(actors, "get_job", lambda job_id: extraction_job if job_id == "extract-1" else compile_job)
    monkeypatch.setattr(actors, "check_cancellation", lambda _job_id: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda _job_id, _owner: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda *_args: None)
    monkeypatch.setattr(actors, "temp_file_path", lambda **_kwargs: str(tmp_path / "source.pdf"))
    monkeypatch.setattr(actors, "download_to_path", lambda _key, path: Path(path).write_bytes(source_bytes))
    monkeypatch.setattr(actors, "cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr(actors, "update_job", lambda _job_id, **fields: updates.append(fields))
    def capture_transport(**kwargs):
        calls.append(deepcopy(kwargs["envelope"]))
        return transport_operation(**kwargs)

    monkeypatch.setattr(actors, "validate_word_edit_transport", capture_transport)
    monkeypatch.setattr(actors, "compile_document", lambda *_args, **_kwargs: pytest.fail("word protocol must not use legacy line compiler"))
    monkeypatch.setattr(actors, "upload_path", lambda *_args, **_kwargs: pytest.fail("unsupported word compile must not upload an artifact"))

    actors.editor_word_edit_compile_job.fn("word-job", envelope)
    assert calls == [envelope]
    final = updates[-1]
    assert final["status"] == JobState.failed
    assert final["error_code"] == "WORD_EDIT_COMPILATION_NOT_IMPLEMENTED"
    assert final["result"]["validated_operation_count"] == 2
    assert "artifact_key" not in final["result"]


def test_failed_word_edit_download_is_rejected_without_streaming(monkeypatch):
    from app.api.tools.editor import router as editor_http

    failed = SimpleNamespace(status=JobState.failed, result={"validated_operation_count": 1})
    monkeypatch.setattr(editor_http, "get_job", lambda _job_id: failed)
    monkeypatch.setattr(editor_http, "stream_object", lambda *_args: pytest.fail("failed jobs cannot stream artifacts"))
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(editor_http.download_compiled_pdf("word-job"))
    assert rejected.value.status_code == 409


def test_only_general_editor_ocr_v2_extraction_freezes_word_baseline(monkeypatch, tmp_path):
    source_bytes = b"baseline source bytes"
    updates = []
    job = SimpleNamespace(status=JobState.queued, owner_identity="owner", payload={})
    monkeypatch.setattr(actors, "get_job", lambda _job_id: job)
    monkeypatch.setattr(actors, "check_cancellation", lambda _job_id: None)
    monkeypatch.setattr(actors, "acquire_lease", lambda *_args: (True, ""))
    monkeypatch.setattr(actors, "release_lease", lambda *_args: None)
    monkeypatch.setattr(actors, "temp_file_path", lambda **_kwargs: str(tmp_path / "source.pdf"))
    monkeypatch.setattr(actors, "download_to_path", lambda _key, path: Path(path).write_bytes(source_bytes))
    monkeypatch.setattr(actors, "cleanup_paths", lambda *_paths: None)
    monkeypatch.setattr(actors, "update_job", lambda _job_id, **fields: updates.append(fields))
    monkeypatch.setattr(actors, "configured_editor_ocr_engine", lambda: "internal")

    def extraction_result():
        result = deepcopy(extraction_fixture()["result"])
        result.pop("word_edit_protocols")
        result.pop("word_edit_baseline")
        return result

    monkeypatch.setattr(actors, "execute_editor_ocr", lambda *_args, **_kwargs: extraction_result())
    monkeypatch.setattr(actors, "execute_studio_editor_extraction", lambda *_args, **_kwargs: extraction_result())
    monkeypatch.setattr(native_operations, "editor_colors", lambda layout, *_args, **_kwargs: layout)

    actors.editor_extract_job.fn("extract-general", "source-key", None, "source.pdf", True, "general_editor")
    general_result = updates[-1]["result"]
    assert updates[-1]["status"] == JobState.succeeded
    assert general_result["schema_version"] == "ocr_v2_editor_layout.v1"
    assert general_result["word_edit_protocols"] == [WORD_EDIT_PROTOCOL]
    assert general_result["word_edit_baseline"]["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert general_result["word_edit_baseline"]["digest"] == baseline_digest(
        "extract-general",
        hashlib.sha256(source_bytes).hexdigest(),
        general_result["word_edit_baseline"]["engine_fingerprint"],
        general_result,
    )

    actors.editor_extract_job.fn("extract-studio", "source-key", None, "source.pdf", True, "studio")
    studio_result = updates[-1]["result"]
    assert updates[-1]["status"] == JobState.succeeded
    assert "word_edit_protocols" not in studio_result
    assert "word_edit_baseline" not in studio_result

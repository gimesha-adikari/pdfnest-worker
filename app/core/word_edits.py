"""Trusted General Editor word-edit protocol validation.

This module validates identity and transport only. It intentionally does not
modify or publish PDFs; precise word compilation is not implemented yet.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

WORD_EDIT_PROTOCOL = "platen_word_edits.v1"
WORD_EDIT_BASELINE_ALGORITHM = "general_editor_word_baseline.v1"
MAX_WORD_EDIT_OPERATIONS = 1000
MAX_WORD_REPLACEMENT_SCALARS = 4096
NATIVE_FRAME_LIMIT = 16 * 1024 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_FIELDS = {
    "edit_protocol", "source_tracker", "extraction_job_id", "baseline_digest", "edits"
}
_OPERATION_FIELDS = {
    "page_num", "element_id", "word_id", "expected_original_text",
    "replacement_text", "style_override", "fit_policy",
}


class WordEditProtocolError(ValueError):
    """A stable, safe-to-report word edit request or baseline rejection."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WordEditProtocolError("word edit data is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def baseline_digest(
    extraction_job_id: str,
    source_sha256: str,
    engine_fingerprint: str,
    result: Mapping[str, Any],
) -> str:
    """Hash the frozen extraction projection and its source/engine identity."""
    content = {
        "extraction_job_id": extraction_job_id,
        "source_sha256": source_sha256,
        "engine_fingerprint": engine_fingerprint,
        "algorithm_version": WORD_EDIT_BASELINE_ALGORITHM,
        "schema_version": result.get("schema_version"),
        "language_mode": result.get("language_mode"),
        "languages": result.get("languages"),
        "pages": result.get("pages"),
    }
    return _sha256(_canonical_json(content))


def build_word_edit_baseline(
    extraction_job_id: str,
    source_sha256: str,
    engine_fingerprint: str,
    result: Mapping[str, Any],
) -> dict[str, str]:
    if not _DIGEST_RE.fullmatch(source_sha256):
        raise WordEditProtocolError("source digest is malformed")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", engine_fingerprint):
        raise WordEditProtocolError("engine fingerprint is malformed")
    return {
        "extraction_job_id": extraction_job_id,
        "source_sha256": source_sha256,
        "engine_fingerprint": engine_fingerprint,
        "algorithm_version": WORD_EDIT_BASELINE_ALGORITHM,
        "digest": baseline_digest(extraction_job_id, source_sha256, engine_fingerprint, result),
    }


def editor_engine_fingerprint(engine: str, language_mode: str, languages: list[str]) -> str:
    details = {
        "engine": engine,
        "language_mode": language_mode,
        "languages": languages,
        "layout_schema": "ocr_v2_editor_layout.v1",
        "algorithm_version": WORD_EDIT_BASELINE_ALGORITHM,
    }
    return _sha256(_canonical_json(details))


def _job_value(job: Any, name: str) -> Any:
    if isinstance(job, Mapping):
        return job.get(name)
    return getattr(job, name, None)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def _text(value: Any, field: str) -> str:
    if type(value) is not str or "\x00" in value:
        raise WordEditProtocolError(f"{field} must be a valid string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise WordEditProtocolError(f"{field} contains invalid Unicode") from exc
    return value


def validate_word_edit_request(request: Any, extraction: Any) -> dict[str, Any]:
    """Revalidate exact v1 operations against the server-held completed job."""
    if type(request) is not dict or set(request) != _TOP_LEVEL_FIELDS:
        raise WordEditProtocolError("word edit request fields are invalid")
    if request.get("edit_protocol") != WORD_EDIT_PROTOCOL:
        raise WordEditProtocolError("unknown word edit protocol")
    source_tracker = _text(request.get("source_tracker"), "source_tracker")
    extraction_job_id = _text(request.get("extraction_job_id"), "extraction_job_id")
    supplied_digest = _text(request.get("baseline_digest"), "baseline_digest")
    if not source_tracker or not extraction_job_id:
        raise WordEditProtocolError("source and extraction job identity are required")
    if _job_value(extraction, "id") != extraction_job_id:
        raise WordEditProtocolError("extraction job identity does not match")
    if _job_value(extraction, "job_type") != "editor_extract" or _status(_job_value(extraction, "status")) != "succeeded":
        raise WordEditProtocolError("trusted extraction baseline is unavailable or incomplete")

    payload = _job_value(extraction, "payload")
    result = _job_value(extraction, "result")
    if type(payload) is not dict or type(result) is not dict:
        raise WordEditProtocolError("trusted extraction baseline is malformed")
    if (
        payload.get("source_key") != source_tracker
        or payload.get("consumer") != "general_editor"
        or payload.get("ocr_v2") is not True
    ):
        raise WordEditProtocolError("source is not bound to a General Editor OCR V2 extraction")
    if result.get("schema_version") != "ocr_v2_editor_layout.v1":
        raise WordEditProtocolError("extraction layout schema is unsupported")
    advertised_protocols = result.get("word_edit_protocols")
    if type(advertised_protocols) is not list or WORD_EDIT_PROTOCOL not in advertised_protocols:
        raise WordEditProtocolError("extraction did not advertise word edit protocol support")

    baseline = result.get("word_edit_baseline")
    expected_baseline_fields = {
        "extraction_job_id", "source_sha256", "engine_fingerprint", "algorithm_version", "digest"
    }
    if type(baseline) is not dict or set(baseline) != expected_baseline_fields:
        raise WordEditProtocolError("trusted word edit baseline is malformed")
    if baseline.get("extraction_job_id") != extraction_job_id:
        raise WordEditProtocolError("baseline extraction identity does not match")
    if baseline.get("algorithm_version") != WORD_EDIT_BASELINE_ALGORITHM:
        raise WordEditProtocolError("baseline algorithm version is unsupported")
    source_sha = baseline.get("source_sha256")
    engine_fp = baseline.get("engine_fingerprint")
    if type(source_sha) is not str or not _DIGEST_RE.fullmatch(source_sha):
        raise WordEditProtocolError("baseline source identity is malformed")
    if type(engine_fp) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", engine_fp):
        raise WordEditProtocolError("baseline engine identity is malformed")
    recomputed = baseline_digest(extraction_job_id, source_sha, engine_fp, result)
    if baseline.get("digest") != recomputed or supplied_digest != recomputed:
        raise WordEditProtocolError("baseline digest is stale or invalid")

    edits = request.get("edits")
    if type(edits) is not list or not 1 <= len(edits) <= MAX_WORD_EDIT_OPERATIONS:
        raise WordEditProtocolError("word edit operation count is invalid")
    targets: dict[tuple[int, str, str], list[str]] = {}
    pages = result.get("pages")
    if type(pages) is not list:
        raise WordEditProtocolError("baseline pages are malformed")
    seen_pages: set[int] = set()
    seen_elements: set[tuple[int, str]] = set()
    for page in pages:
        if type(page) is not dict or type(page.get("page_num")) is not int or type(page.get("elements")) is not list:
            raise WordEditProtocolError("baseline page identity is malformed")
        page_num = page["page_num"]
        if page_num < 1 or page_num in seen_pages:
            raise WordEditProtocolError("baseline page identities are ambiguous")
        seen_pages.add(page_num)
        for element in page["elements"]:
            if type(element) is not dict or type(element.get("id")) is not str:
                raise WordEditProtocolError("baseline element identity is malformed")
            element_key = (page_num, element["id"])
            if not element["id"] or element_key in seen_elements:
                raise WordEditProtocolError("baseline element identities are ambiguous")
            seen_elements.add(element_key)
            words = element.get("word_geometry", [])
            if words is None:
                words = []
            if type(words) is not list:
                raise WordEditProtocolError("baseline word geometry is malformed")
            word_ids = element.get("word_ids")
            if words:
                if type(word_ids) is not list or len(word_ids) != len(words):
                    raise WordEditProtocolError("baseline word identities are inconsistent")
            elif word_ids not in (None, []):
                raise WordEditProtocolError("baseline word identities are inconsistent")
            for index, word in enumerate(words):
                if type(word) is not dict or type(word.get("id")) is not str or type(word.get("text")) is not str:
                    raise WordEditProtocolError("baseline word identity is malformed")
                if not word["id"] or word_ids[index] != word["id"]:
                    raise WordEditProtocolError("baseline word identities are inconsistent")
                key = (page_num, element["id"], word["id"])
                targets.setdefault(key, []).append(word["text"])

    seen: set[tuple[int, str, str]] = set()
    for index, edit in enumerate(edits):
        if type(edit) is not dict or set(edit) != _OPERATION_FIELDS:
            raise WordEditProtocolError(f"edit {index} fields are invalid")
        page_num = edit.get("page_num")
        if type(page_num) is not int or page_num < 1:
            raise WordEditProtocolError(f"edit {index} page number is invalid")
        element_id = _text(edit.get("element_id"), f"edit {index} element_id")
        word_id = _text(edit.get("word_id"), f"edit {index} word_id")
        expected = _text(edit.get("expected_original_text"), f"edit {index} expected_original_text")
        replacement = _text(edit.get("replacement_text"), f"edit {index} replacement_text")
        if not element_id or not word_id:
            raise WordEditProtocolError(f"edit {index} identity is empty")
        if not expected:
            raise WordEditProtocolError(f"edit {index} expected original text is empty")
        if len(replacement) > MAX_WORD_REPLACEMENT_SCALARS:
            raise WordEditProtocolError(f"replacement exceeds {MAX_WORD_REPLACEMENT_SCALARS} Unicode scalar values")
        if edit.get("style_override") is not None:
            raise WordEditProtocolError("style overrides are not supported by protocol v1")
        if edit.get("fit_policy") != "preserve_neighbors":
            raise WordEditProtocolError("unsupported word edit fit policy")
        key = (page_num, element_id, word_id)
        if key in seen:
            raise WordEditProtocolError("duplicate or conflicting word edit target")
        seen.add(key)
        matches = targets.get(key, [])
        if len(matches) != 1:
            raise WordEditProtocolError("word edit target is absent or ambiguous in baseline")
        if matches[0] != expected:
            raise WordEditProtocolError("expected original text does not match baseline")

    # Check the exact encoded IPC request shape before entering the fixed frame.
    kwargs = {"$": "map", "v": [["envelope", _native_encode(request)]]}
    ipc_shape = {
        "operation": "validate_word_edit_transport",
        "args": {"$": "list", "v": []},
        "kwargs": kwargs,
        "callbacks": [],
        "raster_limit": None,
    }
    # Native IPC uses ensure_ascii=True and has other fixed metadata fields.
    encoded_size = len(json.dumps(ipc_shape, ensure_ascii=True).encode("ascii"))
    if encoded_size > NATIVE_FRAME_LIMIT - 4096:
        raise WordEditProtocolError("validated operations exceed the native isolation frame limit")
    return request


def resolve_word_edit_targets(
    validated_request: Mapping[str, Any],
    extraction_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve exact v1 operation identities into server-held page geometry.

    This must only be called after ``validate_word_edit_request``. Coordinates
    are read from the frozen extraction result; the request itself contains no
    authoritative geometry.
    """
    baseline = extraction_result.get("word_edit_baseline")
    pages = extraction_result.get("pages")
    if type(baseline) is not dict or type(pages) is not list:
        raise WordEditProtocolError("trusted extraction geometry is unavailable")
    source_sha = baseline.get("source_sha256")
    baseline_hash = baseline.get("digest")
    if type(source_sha) is not str or not _DIGEST_RE.fullmatch(source_sha):
        raise WordEditProtocolError("trusted source identity is malformed")
    if baseline_hash != validated_request.get("baseline_digest"):
        raise WordEditProtocolError("trusted word edit baseline is stale")

    pages_by_number: dict[int, Mapping[str, Any]] = {}
    for page in pages:
        if type(page) is not dict or type(page.get("page_num")) is not int:
            raise WordEditProtocolError("trusted extraction page geometry is malformed")
        page_num = page["page_num"]
        if page_num < 1 or page_num in pages_by_number:
            raise WordEditProtocolError("trusted extraction page geometry is ambiguous")
        pages_by_number[page_num] = page

    resolved: list[dict[str, Any]] = []
    for edit in validated_request.get("edits", []):
        page = pages_by_number.get(edit.get("page_num"))
        if page is None:
            raise WordEditProtocolError("word edit page is absent from trusted extraction")
        page_width, page_height = page.get("width"), page.get("height")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in (page_width, page_height)):
            raise WordEditProtocolError("trusted extraction page geometry is malformed")
        elements = page.get("elements")
        if type(elements) is not list:
            raise WordEditProtocolError("trusted extraction elements are malformed")
        matching_elements = [element for element in elements if type(element) is dict and element.get("id") == edit.get("element_id")]
        if len(matching_elements) != 1:
            raise WordEditProtocolError("word edit element is absent or ambiguous in trusted extraction")
        words = matching_elements[0].get("word_geometry")
        if type(words) is not list:
            raise WordEditProtocolError("trusted extraction word geometry is malformed")
        matching_words = [word for word in words if type(word) is dict and word.get("id") == edit.get("word_id")]
        if len(matching_words) != 1:
            raise WordEditProtocolError("word edit target is absent or ambiguous in trusted extraction")
        word = matching_words[0]
        if word.get("text") != edit.get("expected_original_text"):
            raise WordEditProtocolError("word edit text does not match trusted geometry")
        x, y, width, height = (word.get(name) for name in ("x", "y", "width", "height"))
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0:
            raise WordEditProtocolError("trusted extraction word geometry is malformed")
        resolved.append({
            "edit": dict(edit),
            "bbox": [float(x), float(y), float(x + width), float(y + height)],
            "page_size": [float(page_width), float(page_height)],
            "source_sha256": source_sha,
            "baseline_digest": baseline_hash,
        })
    return resolved


def native_transport_digest(envelope: Mapping[str, Any]) -> str:
    """Stable digest recorded by the parent and echoed by native isolation."""
    return _sha256(_canonical_json(envelope))


def _native_encode(value: Any) -> Any:
    if type(value) is dict:
        return {"$": "map", "v": [[key, _native_encode(item)] for key, item in value.items()]}
    if type(value) is list:
        return {"$": "list", "v": [_native_encode(item) for item in value]}
    return value


def validate_word_edit_transport(*, input_path: str | Path, envelope: dict[str, Any]) -> dict[str, Any]:
    """Isolated no-op receipt proving the validated envelope crossed the boundary."""
    source_digest = hashlib.sha256()
    with Path(input_path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            source_digest.update(chunk)
    digest = native_transport_digest(envelope)
    return {
        "protocol": envelope.get("edit_protocol"),
        "extraction_job_id": envelope.get("extraction_job_id"),
        "baseline_digest": envelope.get("baseline_digest"),
        "operation_count": len(envelope.get("edits", [])),
        "transport_digest": digest,
        "source_sha256": source_digest.hexdigest(),
    }

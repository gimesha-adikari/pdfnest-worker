"""Privacy-filtered internal diagnostics for candidate validation refusals.

These summaries are intended for worker logs and test evidence only. They never
include source text, word IDs, storage keys, or PDF content.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

_ALLOWED_CHECKS = frozenset({
    "page_count",
    "geometry",
    "image_streams_preserved",
    "neighbors_preserved",
    "outside_pixels_clear",
    "target_content_valid",
    "text_reading_order_preserved",
})
_UNEDITED_PAGE_CHECK = re.compile(r"unedited_page_[1-9][0-9]*_(?:72|144|288)_identical\Z")
_ALLOWED_DPIS = frozenset({"72", "144", "288"})


def summarize_candidate_validation_failure(job_id: str, result: Any) -> dict[str, Any] | None:
    """Return a safe validation summary, or None for other compiler results."""
    if type(result) is not dict or result.get("error_code") != "CANDIDATE_VALIDATION_FAILED":
        return None

    checks = result.get("checks")
    if type(checks) is not dict:
        checks = {}

    failed_checks: list[str] = []
    if checks.get("valid") is False:
        failed_checks.append("candidate_valid")
    for name in _ALLOWED_CHECKS:
        if checks.get(name) is False:
            failed_checks.append(name)
    for name, value in checks.items():
        if isinstance(name, str) and _UNEDITED_PAGE_CHECK.fullmatch(name) and value is False:
            failed_checks.append(name)

    raw_counts = checks.get("outside_changed_pixels_by_dpi")
    pixel_counts: dict[str, int] = {}
    if type(raw_counts) is dict:
        for dpi in sorted(_ALLOWED_DPIS):
            value = raw_counts.get(dpi)
            if type(value) is int and 0 <= value <= 1_000_000_000:
                pixel_counts[dpi] = value

    raw_targets = checks.get("target_results")
    targets: list[dict[str, Any]] = []
    if type(raw_targets) is list:
        for target in raw_targets[:16]:
            if type(target) is not dict:
                continue
            raw_hits = target.get("replacement_hits_at_target")
            hits_count = len(raw_hits) if type(raw_hits) is list else 0
            raw_old_hits = target.get("old_target_hits_at_target")
            old_hits_count = (
                raw_old_hits if type(raw_old_hits) is int and 0 <= raw_old_hits <= 1_000_000
                else 0
            )
            searchable = target.get("replacement_searchable")
            targets.append({
                "replacement_searchable": searchable if type(searchable) is bool else False,
                "replacement_hits_at_target_count": min(hits_count, 1_000_000),
                "old_target_hits_at_target": old_hits_count,
            })

    job_digest = hashlib.sha256(str(job_id).encode("utf-8", errors="replace")).hexdigest()
    summary = {
        "schema_version": 1,
        "job_id_sha256_prefix": job_digest[:16],
        "failed_checks": sorted(set(failed_checks)),
        "outside_changed_pixels_by_dpi": pixel_counts,
        "target_summaries": targets,
    }
    raw_neighbors = checks.get("neighbor_diagnostics")
    if type(raw_neighbors) is dict and set(raw_neighbors) == {
        "source_word_count", "candidate_word_count", "text_count_mismatch",
        "bbox_mismatch_count", "max_bbox_delta_points",
    }:
        integer_fields = (
            "source_word_count", "candidate_word_count",
            "text_count_mismatch", "bbox_mismatch_count",
        )
        if all(type(raw_neighbors.get(key)) is int and 0 <= raw_neighbors[key] <= 1_000_000 for key in integer_fields):
            delta = raw_neighbors.get("max_bbox_delta_points")
            if type(delta) in (int, float) and math.isfinite(delta) and 0 <= delta <= 10_000:
                summary["neighbor_diagnostics"] = {
                    key: raw_neighbors[key] for key in integer_fields
                } | {"max_bbox_delta_points": round(float(delta), 4)}
    return summary

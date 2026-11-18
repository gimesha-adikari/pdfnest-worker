from __future__ import annotations

import json

from app.core.candidate_validation_diagnostics import summarize_candidate_validation_failure


def test_candidate_validation_diagnostics_only_emits_allowlisted_summaries() -> None:
    result = {
        "error_code": "CANDIDATE_VALIDATION_FAILED",
        "source_path": "/private/source.pdf",
        "checks": {
            "valid": False,
            "page_count": True,
            "geometry": True,
            "image_streams_preserved": True,
            "neighbors_preserved": False,
            "outside_pixels_clear": False,
            "outside_changed_pixels_by_dpi": {"72": 0, "144": 7, "288": 0, "evil": 99},
            "target_content_valid": False,
            "text_reading_order_preserved": True,
            "neighbor_diagnostics": {
                "source_word_count": 8,
                "candidate_word_count": 8,
                "text_count_mismatch": 0,
                "bbox_mismatch_count": 2,
                "max_bbox_delta_points": 0.125,
                "source_text": "must not escape",
                "source_boxes": [[1, 2, 3, 4]],
            },
            "target_results": [{
                "element_id": "private-element-id",
                "word_id": "private-word-id",
                "source_text": "Confidential source text",
                "replacement_text": "Sensitive replacement",
                "replacement_searchable": False,
                "replacement_hits_at_target": ["Sensitive replacement"],
                "old_target_hits_at_target": 2,
                "storage_key": "private/object/key",
            }],
        },
    }

    summary = summarize_candidate_validation_failure("secret-job-id", result)
    assert summary is not None
    assert summary["failed_checks"] == [
        "candidate_valid",
        "neighbors_preserved",
        "outside_pixels_clear",
        "target_content_valid",
    ]
    assert summary["outside_changed_pixels_by_dpi"] == {"144": 7, "288": 0, "72": 0}
    assert summary["target_summaries"] == [{
        "replacement_searchable": False,
        "replacement_hits_at_target_count": 1,
        "old_target_hits_at_target": 2,
    }]
    assert summary["neighbor_diagnostics"] == {
        "source_word_count": 8,
        "candidate_word_count": 8,
        "text_count_mismatch": 0,
        "bbox_mismatch_count": 2,
        "max_bbox_delta_points": 0.125,
    }
    serialized = json.dumps(summary)
    for secret in (
        "secret-job-id",
        "Confidential source text",
        "Sensitive replacement",
        "private-element-id",
        "private-word-id",
        "/private/source.pdf",
        "private/object/key",
        "source_boxes",
    ):
        assert secret not in serialized


def test_candidate_validation_diagnostics_ignores_other_refusals() -> None:
    assert summarize_candidate_validation_failure(
        "job", {"error_code": "UNSUPPORTED_GEOMETRY", "checks": {"geometry": False}}
    ) is None


def test_candidate_validation_diagnostics_bounds_dynamic_checks_and_counts() -> None:
    result = {
        "error_code": "CANDIDATE_VALIDATION_FAILED",
        "checks": {
            "valid": False,
            "outside_changed_pixels_by_dpi": {"144": -1, "288": 2_000_000_000, "72": 12},
            "unedited_page_2_144_identical": False,
            "unedited_page_2_300_identical": False,
            "source_text": "must not escape",
        },
    }
    summary = summarize_candidate_validation_failure("job", result)
    assert summary is not None
    assert summary["failed_checks"] == ["candidate_valid", "unedited_page_2_144_identical"]
    assert summary["outside_changed_pixels_by_dpi"] == {"72": 12}
    assert summary["target_summaries"] == []

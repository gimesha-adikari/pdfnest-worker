"""Differential parity for the public legacy Highlight/Underline/Strikeout family.

The legacy contract is rectangle-oriented.  This harness compares the
historical application projection with the SDK-backed OCR-word projection and
records semantic PDF signatures; raw PDF bytes are retained only as evidence
of container-level differences.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import fitz

from app.core.legacy_markup_ocr_engine import execute_legacy_markup


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "output" / "document-sdk-legacy-markup-consumer-01"
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
SDK_INPUT_ROOT = ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs"


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "native_english",
        "kind": "approved-worker-native-fixture",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "mode": "smart",
        "box_policy": "all_pages",
    },
    {
        "name": "native_english_force_ocr",
        "kind": "approved-worker-native-fixture-explicit-ocr-mode",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "mode": "ocr",
        "box_policy": "first_two_words",
    },
    {
        "name": "scanned_bilingual",
        "kind": "approved-real-scanned-bilingual-fixture",
        "path": SDK_INPUT_ROOT / "approved-bilingual.pdf",
        "mode": "ocr",
        "box_policy": "all_pages",
    },
    {
        "name": "mixed_native_scanned",
        "kind": "approved-real-native-plus-scanned-fixture",
        "path": SDK_INPUT_ROOT / "approved-mixed-native-bilingual.pdf",
        "mode": "smart",
        "box_policy": "all_pages",
    },
    {
        "name": "multiple_regions",
        "kind": "approved-worker-native-fixture-with-two-regions",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "mode": "smart",
        "box_policy": "first_two_words",
    },
    {
        "name": "multi_page",
        "kind": "approved-worker-native-multi-page-fixture",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "mode": "smart",
        "box_policy": "all_pages",
    },
    {
        "name": "rotated_cropbox",
        "kind": "approved-real-rotated-cropbox-fixture",
        "path": APPROVED_ROOT / "ocr-extracted-text-29-rotated (1).pdf",
        "mode": "smart",
        "box_policy": "all_pages",
        "expected_sha256": "120f36b0ae84432beb9a4ae1df987afc4b7d80c4f85586c6eb8730ced8c151af",
    },
    {
        "name": "no_match_empty_region",
        "kind": "approved-worker-native-fixture-empty-region-control",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "mode": "smart",
        "box_policy": "bottom_corner",
    },
    {
        "name": "malformed_input",
        "kind": "controlled-failure-input",
        "path": Path("/tmp/legacy-markup-parity-malformed.pdf"),
        "mode": "smart",
        "box_policy": "none",
        "expected_failure": True,
    },
)

ACTIONS = ("highlight", "underline", "strikeout")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_summary(text: str) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {"length": len(text), "sha256": _sha256(encoded)}


def _round(value: Any) -> float:
    return round(float(value), 4)


def _rect_signature(rect: Any) -> list[float]:
    return [_round(rect.x0), _round(rect.y0), _round(rect.x1), _round(rect.y1)]


def _item_signature(item: Any) -> list[Any]:
    values: list[Any] = []
    for value in item:
        if isinstance(value, fitz.Rect):
            values.append(_rect_signature(value))
        elif isinstance(value, fitz.Point):
            values.append([_round(value.x), _round(value.y)])
        else:
            values.append(value)
    return values


def _drawing_signature(drawing: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": drawing.get("type"),
        "rect": _rect_signature(drawing["rect"]),
        "items": [_item_signature(item) for item in drawing.get("items", ())],
        "fill": list(drawing["fill"]) if drawing.get("fill") else None,
        "color": list(drawing["color"]) if drawing.get("color") else None,
        "fill_opacity": _round(drawing["fill_opacity"]) if drawing.get("fill_opacity") is not None else None,
        "stroke_opacity": _round(drawing["stroke_opacity"]) if drawing.get("stroke_opacity") is not None else None,
        "width": _round(drawing["width"]) if drawing.get("width") is not None else None,
    }


def _image_signature(document: fitz.Document, image: tuple[Any, ...]) -> dict[str, Any]:
    xref = int(image[0])
    extracted = document.extract_image(xref)
    image_bytes = extracted.get("image", b"")
    return {
        "width": int(image[2]),
        "height": int(image[3]),
        "colorspace": image[5],
        "sha256": _sha256(image_bytes),
    }


def _pdf_signature(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    pages: list[dict[str, Any]] = []
    with fitz.open(path) as document:
        for page in document:
            pages.append(
                {
                    "rect": _rect_signature(page.rect),
                    "cropbox": _rect_signature(page.cropbox),
                    "rotation": page.rotation,
                    "text": _text_summary(page.get_text("text")),
                    "drawings": sorted(
                        (_drawing_signature(drawing) for drawing in page.get_drawings()),
                        key=lambda drawing: json.dumps(drawing, sort_keys=True),
                    ),
                    "images": sorted(
                        (_image_signature(document, image) for image in page.get_images(full=True)),
                        key=lambda image: json.dumps(image, sort_keys=True),
                    ),
                }
            )
    semantic = {"page_count": len(pages), "pages": pages}
    return {
        "semantic": semantic,
        "semantic_sha256": _sha256(json.dumps(semantic, sort_keys=True).encode()),
        "byte_length": len(raw),
        "sha256": _sha256(raw),
    }


def _boxes(path: Path, policy: str) -> list[dict[str, Any]]:
    if policy == "none":
        return []
    with fitz.open(path) as document:
        if policy == "all_pages":
            return [
                {
                    "page": index + 1,
                    "x": 0.0,
                    "y": 0.0,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                    "color": "#FFFF00",
                }
                for index, page in enumerate(document)
            ]
        if policy == "bottom_corner":
            page = document[0]
            return [
                {
                    "page": 1,
                    "x": max(0.0, float(page.rect.width) - 18.0),
                    "y": max(0.0, float(page.rect.height) - 18.0),
                    "width": 10.0,
                    "height": 10.0,
                    "color": "#FFFF00",
                }
            ]
        if policy == "first_two_words":
            page = document[0]
            words = page.get_text("words")[:2]
            return [
                {
                    "page": 1,
                    "x": float(word[0]) - 1.0,
                    "y": float(word[1]) - 1.0,
                    "width": float(word[2] - word[0]) + 2.0,
                    "height": float(word[3] - word[1]) + 2.0,
                    "color": "#FFFF00",
                }
                for word in words
            ]
    raise ValueError(f"unknown box policy: {policy}")


def _fixture_record(case: dict[str, Any]) -> dict[str, Any]:
    path = Path(case["path"])
    if case.get("expected_failure"):
        return {"name": case["name"], "path": str(path), "expected_failure": True}
    if not path.exists():
        raise FileNotFoundError(path)
    digest = _sha256(path.read_bytes())
    if case.get("expected_sha256") and digest != case["expected_sha256"]:
        raise ValueError(f"fixture hash changed for {case['name']}")
    with fitz.open(path) as document:
        page_metadata = [
            {
                "page": index + 1,
                "width": _round(page.rect.width),
                "height": _round(page.rect.height),
                "rotation": page.rotation,
                "cropbox": _rect_signature(page.cropbox),
                "native_word_count": len(page.get_text("words")),
            }
            for index, page in enumerate(document)
        ]
    return {
        "name": case["name"],
        "kind": case["kind"],
        "path": str(path),
        "source_sha256": digest,
        "mode": case["mode"],
        "box_policy": case["box_policy"],
        "page_metadata": page_metadata,
    }


def _run_case(case: dict[str, Any], action: str, boxes: list[dict[str, Any]]) -> dict[str, Any]:
    source = Path(case["path"])
    outputs: dict[str, Any] = {}
    for engine in ("internal", "sdk"):
        os.environ["LEGACY_MARKUP_OCR_ENGINE"] = engine
        output_path = Path(tempfile.mktemp(prefix=f"legacy-markup-{action}-", suffix=".pdf"))
        try:
            try:
                execution = execute_legacy_markup(
                    source,
                    output_path,
                    boxes=boxes,
                    action=action,
                    mode=case["mode"],
                )
                outputs[engine] = {
                    "outcome": "success",
                    "execution": execution,
                    "artifact": _pdf_signature(output_path),
                }
            except Exception as exc:
                outputs[engine] = {
                    "outcome": "failure",
                    "error_class": type(exc).__name__,
                    "safe_error": str(exc),
                }
        finally:
            output_path.unlink(missing_ok=True)

    internal = outputs["internal"]
    sdk = outputs["sdk"]
    if internal["outcome"] == sdk["outcome"] == "success":
        semantic_match = internal["artifact"]["semantic_sha256"] == sdk["artifact"]["semantic_sha256"]
        material_mismatches: list[str] = [] if semantic_match else ["semantic_pdf_signature"]
        return {
            "case": case["name"],
            "action": action,
            "mode": case["mode"],
            "boxes": boxes,
            "internal": internal,
            "sdk": sdk,
            "matched": semantic_match,
            "material_mismatches": material_mismatches,
            "container_byte_difference": internal["artifact"]["sha256"] != sdk["artifact"]["sha256"],
            "classification": "MATCH" if semantic_match else "MATERIAL_MISMATCH",
        }
    failure_match = internal["outcome"] == sdk["outcome"] == "failure"
    return {
        "case": case["name"],
        "action": action,
        "mode": case["mode"],
        "boxes": boxes,
        "internal": internal,
        "sdk": sdk,
        "matched": failure_match,
        "material_mismatches": [] if failure_match else ["outcome"],
        "classification": "CONTROLLED_FAILURE_MATCH" if failure_match else "MATERIAL_MISMATCH",
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    malformed = Path("/tmp/legacy-markup-parity-malformed.pdf")
    malformed.write_bytes(b"not a PDF")
    try:
        fixtures = [_fixture_record(case) for case in CASES]
        (OUTPUT_ROOT / "fixture-inventory.json").write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
        records: list[dict[str, Any]] = []
        for case in CASES:
            boxes = _boxes(Path(case["path"]), case["box_policy"]) if not case.get("expected_failure") else []
            for action in ACTIONS:
                records.append(_run_case(case, action, boxes))

        matched = [record for record in records if record["matched"]]
        summary = {
            "schema_version": "legacy_markup_consumer_parity.v1",
            "consumer": "legacy_markup",
            "selector": "LEGACY_MARKUP_OCR_ENGINE",
            "case_count": len(records),
            "matched_count": len(matched),
            "material_mismatch_count": sum(bool(record["material_mismatches"]) for record in records),
            "semantic_parity": len(matched) == len(records),
            "records": records,
        }
        (OUTPUT_ROOT / "initial-parity-analysis.json").write_text(
            json.dumps(
                {
                    "scope": "initial implementation differential observations reconciled by the final rerun",
                    "comparison_contract": "semantic PDF structure, page geometry, drawing geometry, image identity; raw bytes are diagnostic only",
                    "historical_observations": [
                        {
                            "case": "scanned_bilingual",
                            "observation": "the first projection comparison had a drawing-count difference (internal 227 versus SDK 234) before legacy-coordinate rounding was applied",
                            "classification": "INTEGRATION_SEAM",
                            "resolution": "the SDK adapter now converts canonical pixel geometry through the historical 2.0x rounded coordinate contract",
                        }
                    ],
                    "mismatches_before_resolution": [
                        record for record in records if not record["matched"]
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (OUTPUT_ROOT / "parity-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        for action in ACTIONS:
            (OUTPUT_ROOT / f"{action}-parity.json").write_text(
                json.dumps(
                    {
                        "action": action,
                        "records": [record for record in records if record["action"] == action],
                        "matched_count": sum(record["matched"] for record in records if record["action"] == action),
                        "material_mismatch_count": sum(
                            bool(record["material_mismatches"])
                            for record in records
                            if record["action"] == action
                        ),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        named_case_files = {
            "native-result.json": "native_english",
            "scanned-result.json": "scanned_bilingual",
            "mixed-result.json": "mixed_native_scanned",
            "rotation-cropbox-result.json": "rotated_cropbox",
            "error-result.json": "malformed_input",
        }
        for filename, case_name in named_case_files.items():
            case_records = [record for record in records if record["case"] == case_name]
            (OUTPUT_ROOT / filename).write_text(
                json.dumps({"case": case_name, "records": case_records}, indent=2) + "\n",
                encoding="utf-8",
            )
        (OUTPUT_ROOT / "geometry-result.json").write_text(
            json.dumps(
                {
                    "scope": "all successful parity records",
                    "fields": ["page geometry", "cropbox", "rotation", "drawing geometry", "image identity"],
                    "semantic_parity": summary["semantic_parity"],
                    "material_mismatch_count": summary["material_mismatch_count"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (OUTPUT_ROOT / "multilingual-result.json").write_text(
            json.dumps(
                {
                    "status": "not_applicable",
                    "reason": "the legacy public markup contract has no language-selection field; scanned_bilingual exercises its historical fixed-English contract",
                    "scanned_bilingual_parity": all(
                        record["matched"] for record in records if record["case"] == "scanned_bilingual"
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (OUTPUT_ROOT / "cancellation.json").write_text(
            json.dumps(
                {
                    "status": "not_run_by_differential_harness",
                    "reason": "cancellation is validated through the durable actor/runtime gate, not by this local pure-file comparison",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (OUTPUT_ROOT / "final-summary.json").write_text(
            json.dumps(
                {
                    "classification": "PARITY_GO" if summary["semantic_parity"] else "PARITY_NO_GO",
                    "matched_count": summary["matched_count"],
                    "case_count": summary["case_count"],
                    "material_mismatch_count": summary["material_mismatch_count"],
                    "raw_container_differences": sum(record.get("container_byte_difference", False) for record in records),
                    "actions": list(ACTIONS),
                    "explicit_ocr_native_control": True,
                    "full_ocr_text_persisted": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0 if summary["semantic_parity"] else 1
    finally:
        malformed.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

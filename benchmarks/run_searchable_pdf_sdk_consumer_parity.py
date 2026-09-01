"""Run the focused PDFNest Searchable PDF internal-versus-SDK parity gate.

This is a consumer-boundary harness, not the historical standalone extraction
parity run. It records contract-safe summaries (hashes, counts, geometry, and
artifact facts) rather than document contents or credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pymupdf as fitz

from app.core.ocr_v2.image_pages import build_image_source_pdf
from app.core.searchable_pdf_engine import execute_searchable_pdf


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "document-sdk-searchable-pdf-consumer-01"


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "single_english_explicit",
        "kind": "approved-exact-english-image",
        "image_paths": (APPROVED_ROOT / "images" / "6670c2153.png",),
        "language": "eng",
        "language_mode": "EXPLICIT",
        "languages": ("eng",),
    },
    {
        "name": "single_sinhala_explicit",
        "kind": "approved-sinhala-image",
        "image_paths": (APPROVED_ROOT / "images" / "1q.jpeg",),
        "language": "sin",
        "language_mode": "EXPLICIT",
        "languages": ("sin",),
    },
    {
        "name": "single_bilingual_explicit",
        "kind": "approved-bilingual-image",
        "image_paths": (APPROVED_ROOT / "images" / "1.jpeg",),
        "language": "eng+sin",
        "language_mode": "EXPLICIT",
        "languages": ("eng", "sin"),
    },
    {
        "name": "single_bilingual_auto",
        "kind": "approved-bilingual-image-auto",
        "image_paths": (APPROVED_ROOT / "images" / "1.jpeg",),
        "language": "auto",
        "language_mode": "AUTO",
        "languages": ("eng", "sin"),
    },
    {
        "name": "ordered_two_bilingual",
        "kind": "approved-two-image-ordered-inputs",
        "image_paths": tuple(APPROVED_ROOT / "images" / f"{index}.jpeg" for index in range(1, 3)),
        "language": "eng+sin",
        "language_mode": "EXPLICIT",
        "languages": ("eng", "sin"),
    },
    {
        "name": "ordered_three_bilingual",
        "kind": "approved-three-image-ordered-inputs",
        "image_paths": tuple(APPROVED_ROOT / "images" / f"{index}.jpeg" for index in range(1, 4)),
        "language": "eng+sin",
        "language_mode": "EXPLICIT",
        "languages": ("eng", "sin"),
    },
    {
        "name": "ordered_four_bilingual",
        "kind": "approved-four-image-ordered-inputs",
        "image_paths": tuple(APPROVED_ROOT / "images" / f"{index}.jpeg" for index in range(1, 5)),
        "language": "eng+sin",
        "language_mode": "EXPLICIT",
        "languages": ("eng", "sin"),
    },
)


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _language_summary(language: Any) -> dict[str, Any]:
    return {
        "requested": list(getattr(language, "requested_languages", ())),
        "detected": list(getattr(language, "detected_languages", ())),
        "status": getattr(language, "language_status", ""),
        "scripts": list(getattr(language, "detected_scripts", ())),
        "script_status": getattr(language, "script_status", ""),
        "mode": getattr(language, "requested_mode", ""),
        "confidence": getattr(language, "detection_confidence", None),
        "reason": getattr(language, "detection_reason", None),
    }


def _result_summary(result: Any) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for page in result.pages:
        token_signature = "|".join(
            f"{token.text}\x1f{token.bbox.x:.4f},{token.bbox.y:.4f},{token.bbox.x1:.4f},{token.bbox.y1:.4f}"
            for token in page.tokens
        )
        pages.append(
            {
                "page_index": page.page_index,
                "classification": _enum_value(page.content_classification),
                "processing_source": _enum_value(page.processing_source),
                "status": _enum_value(page.status),
                "geometry": {
                    "width": round(float(page.geometry.width), 4),
                    "height": round(float(page.geometry.height), 4),
                    "rotation": page.geometry.rotation,
                    "coordinate_space": page.geometry.coordinate_space,
                },
                "text": {"length": len(page.text), "sha256": _sha256(page.text)},
                "token_count": len(page.tokens),
                "token_signature_sha256": _sha256(token_signature),
                "line_count": len(page.lines),
                "block_count": len(page.blocks),
                "reading_order_count": len(page.reading_order),
                "reading_order_sha256": _sha256("|".join(page.reading_order)),
                "language": _language_summary(page.language),
                "capabilities": sorted(page.capabilities),
                "validation_valid": page.validation.valid,
                "failure_code": page.failure_code,
            }
        )
    return {
        "schema_version": result.schema_version,
        "page_count": len(result.pages),
        "pages": pages,
        "capabilities": sorted(result.capabilities),
        "provenance": sorted(
            {
                (item.producer_id, item.producer_version, item.source)
                for item in result.provenance
            }
        ),
        "validation_valid": result.validation.valid,
        "validation_issue_codes": [issue.code for issue in result.validation.issues],
    }


def _artifact_summary(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    pages: list[dict[str, Any]] = []
    with fitz.open(str(path)) as document:
        for page in document:
            pixmap = page.get_pixmap(alpha=False)
            words = page.get_text("words")
            word_signature = "|".join(
                f"{word[4]}\x1f{word[0]:.4f},{word[1]:.4f},{word[2]:.4f},{word[3]:.4f}"
                for word in words
            )
            pages.append(
                {
                    "width": round(float(page.rect.width), 4),
                    "height": round(float(page.rect.height), 4),
                    "image_count": len(page.get_images(full=True)),
                    "word_count": len(words),
                    "text_sha256": _sha256(page.get_text("text")),
                    "word_signature_sha256": _sha256(word_signature),
                    "raster": {
                        "width": pixmap.width,
                        "height": pixmap.height,
                        "sha256": _sha256(pixmap.samples),
                    },
                }
            )
    return {
        "header_valid": data[:5] == b"%PDF-",
        "byte_length": len(data),
        "sha256": _sha256(data),
        "page_count": len(pages),
        "pages": pages,
    }


def _artifact_semantics(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in summary.items() if key not in {"byte_length", "sha256"}}


def _differing_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    """Return safe summary paths whose values differ."""

    if type(left) is not type(right):
        return [prefix or "$"]
    if isinstance(left, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_differing_paths(left[key], right[key], child))
        return paths
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix or "$"]
        paths = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            paths.extend(_differing_paths(left_item, right_item, f"{prefix}[{index}]"))
        return paths
    return [] if left == right else [prefix or "$"]


@contextmanager
def _selected_engine(name: str) -> Iterator[None]:
    previous = os.environ.get("SEARCHABLE_PDF_ENGINE")
    os.environ["SEARCHABLE_PDF_ENGINE"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SEARCHABLE_PDF_ENGINE", None)
        else:
            os.environ["SEARCHABLE_PDF_ENGINE"] = previous


def _path_metadata(path: Path) -> dict[str, Any]:
    if path.is_relative_to(APPROVED_ROOT):
        path_label = f"approved-real-document/{path.relative_to(APPROVED_ROOT)}"
    elif path.is_relative_to(ROOT):
        path_label = str(path.relative_to(ROOT))
    else:
        path_label = f"approved-external-fixture/{path.name}"
    return {
        "path_label": path_label,
        "source_bytes": path.stat().st_size,
        "source_sha256": _sha256(path.read_bytes()),
    }


def _case_source(case: dict[str, Any], temporary: Path) -> tuple[Path, dict[str, Any]]:
    if "path" in case:
        path = Path(case["path"])
        return path, _path_metadata(path)

    image_paths = tuple(Path(item) for item in case["image_paths"])
    image_metadata = [_path_metadata(path) for path in image_paths]
    temporary.mkdir(parents=True, exist_ok=True)
    source = temporary / "ordered-images.pdf"
    build_image_source_pdf(image_paths, source)
    return source, {
        "generated_source": _path_metadata(source),
        "image_inputs": image_metadata,
        "order_preserved": [item["path_label"] for item in image_metadata],
    }


def _run_case(case: dict[str, Any], source: Path, engine_name: str, output: Path) -> dict[str, Any]:
    started = time.perf_counter()
    progress: list[tuple[int, int, int, str]] = []
    try:
        with _selected_engine(engine_name):
            result = execute_searchable_pdf(
                source,
                output,
                language=case["language"],
                language_mode=case["language_mode"],
                languages=case["languages"],
                page_progress_callback=lambda done, total, page: progress.append(
                    (done, total, page.page_index, _enum_value(page.status))
                ),
            )
        result_summary = _result_summary(result)
        artifact_summary = _artifact_summary(output)
        return {
            "engine": engine_name,
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "progress": progress,
            "result": result_summary,
            "artifact": artifact_summary,
        }
    except Exception as exc:
        return {
            "engine": engine_name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "progress": progress,
            "error": {
                "type": type(exc).__name__,
                "substage": getattr(exc, "substage", None),
                "reason_code": getattr(exc, "reason_code", None),
            },
        }


def _environment_metadata() -> dict[str, Any]:
    import platen_document
    from platen_document import DocumentProcessor

    doctor = DocumentProcessor().capabilities()
    revisions: dict[str, str] = {}
    for name, path in (("worker", ROOT / "pdfnest-worker"), ("sdk", ROOT / "platen-document")):
        try:
            revisions[name] = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            revisions[name] = "unavailable"
    return {
        "schema_version": "pdfnest_searchable_pdf_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "revisions": revisions,
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; direct worker consumer boundary; no PDFNest services; no managed resources",
        "searchable_pdf_engine_default": "internal",
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fixtures: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pdfnest-searchable-consumer-parity-") as temporary_dir:
        temporary = Path(temporary_dir)
        for case in CASES:
            source, source_metadata = _case_source(case, temporary / case["name"])
            fixture = {
                "name": case["name"],
                "kind": case["kind"],
                "source": source_metadata,
                "language": case["language"],
                "language_mode": case["language_mode"],
                "languages": list(case["languages"]),
            }
            fixtures.append(fixture)
            internal = _run_case(case, source, "internal", temporary / f"{case['name']}-internal.pdf")
            sdk = _run_case(case, source, "sdk", temporary / f"{case['name']}-sdk.pdf")
            result_differences: list[str] = []
            artifact_differences: list[str] = []
            error_differences: list[str] = []
            if internal.get("ok") and sdk.get("ok"):
                result_differences = _differing_paths(internal["result"], sdk["result"], "result")
                artifact_differences = _differing_paths(
                    _artifact_semantics(internal["artifact"]),
                    _artifact_semantics(sdk["artifact"]),
                    "artifact",
                )
                matched = not result_differences and not artifact_differences
            elif not internal.get("ok") and not sdk.get("ok"):
                error_differences = _differing_paths(internal.get("error"), sdk.get("error"), "error")
                matched = not error_differences
            else:
                error_differences = ["execution.ok"]
                matched = False
            artifact_byte_difference = None
            if internal.get("ok") and sdk.get("ok"):
                artifact_byte_difference = {
                    "byte_length_equal": internal["artifact"]["byte_length"] == sdk["artifact"]["byte_length"],
                    "sha256_equal": internal["artifact"]["sha256"] == sdk["artifact"]["sha256"],
                }
            cases.append(
                {
                    "fixture": fixture,
                    "internal": internal,
                    "sdk": sdk,
                    "parity": "MATCH" if matched else "MISMATCH",
                    "matched_fields": {
                        "result": not result_differences,
                        "artifact_semantics": not artifact_differences,
                        "failure_contract": not error_differences,
                    },
                    "differing_fields": {
                        "result": result_differences,
                        "artifact_semantics": artifact_differences,
                        "failure_contract": error_differences,
                    },
                    "artifact_byte_difference": artifact_byte_difference,
                }
            )

    summary = {
        "schema_version": "pdfnest_searchable_pdf_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; one consumer-specific selector; no PDFNest services; no managed resources",
        "engines": ["internal", "sdk"],
        "case_count": len(cases),
        "matched_cases": sum(item["parity"] == "MATCH" for item in cases),
        "mismatched_cases": sum(item["parity"] != "MATCH" for item in cases),
        "material_mismatches": 0 if all(item["parity"] == "MATCH" for item in cases) else "REVIEW_REQUIRED",
        "cases": cases,
    }
    (OUTPUT_ROOT / "fixture-inventory.json").write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "parity-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "run-metadata.json").write_text(json.dumps(_environment_metadata(), indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases")}, sort_keys=True))
    return 0 if summary["mismatched_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

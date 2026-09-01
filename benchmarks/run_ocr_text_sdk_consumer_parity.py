"""Run the focused PDFNest OCR Text V2 internal-versus-SDK parity check.

The harness deliberately exercises the PDFNest-side consumer boundary rather
than the SDK extraction harness.  It writes only fixture metadata and
contract-safe result summaries; OCR text is represented by lengths and hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.core.ocr_text_engine import execute_ocr_text


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "ocr-text-sdk-first-consumer-01"


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "native_english",
        "kind": "native-text",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "language": "eng",
        "language_mode": "EXPLICIT",
        "languages": ("eng",),
        "routing_policy": "AUTO",
    },
    {
        "name": "scanned_english",
        "kind": "approved-real-derived-english",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-english.pdf",
        "language": "eng",
        "language_mode": "EXPLICIT",
        "languages": ("eng",),
        "routing_policy": "FAST",
    },
    {
        "name": "sinhala",
        "kind": "approved-real-derived-sinhala",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-sinhala.pdf",
        "language": "sin",
        "language_mode": "EXPLICIT",
        "languages": ("sin",),
        "routing_policy": "FAST",
    },
    {
        "name": "bilingual_explicit",
        "kind": "approved-real-derived-bilingual",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-bilingual.pdf",
        "language": "eng+sin",
        "language_mode": "EXPLICIT",
        "languages": ("eng", "sin"),
        "routing_policy": "FAST",
    },
    {
        "name": "bilingual_auto",
        "kind": "approved-real-derived-bilingual-auto",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-bilingual.pdf",
        "language": "auto",
        "language_mode": "AUTO",
        "languages": ("eng", "sin"),
        "routing_policy": "FAST",
    },
    {
        "name": "mixed_auto",
        "kind": "approved-real-derived-native-plus-bilingual",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-mixed-native-bilingual.pdf",
        "language": "auto",
        "language_mode": "AUTO",
        "languages": ("eng", "sin"),
        "routing_policy": "FAST",
    },
    {
        "name": "current_scanned_markdown_fixture",
        "kind": "approved-real-scanned-markdown-fixture",
        "path": APPROVED_ROOT / "ocr-extracted-text-29-rotated (1).pdf",
        "language": "eng",
        "language_mode": "EXPLICIT",
        "languages": ("eng",),
        "routing_policy": "FAST",
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
    }


@contextmanager
def _selected_engine(name: str) -> Iterator[None]:
    previous = os.environ.get("OCR_TEXT_ENGINE")
    os.environ["OCR_TEXT_ENGINE"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OCR_TEXT_ENGINE", None)
        else:
            os.environ["OCR_TEXT_ENGINE"] = previous


def _fixture_metadata(case: dict[str, Any]) -> dict[str, Any]:
    path = Path(case["path"])
    return {
        "name": case["name"],
        "kind": case["kind"],
        "path_label": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else "approved-real-document-boundary",
        "source_bytes": path.stat().st_size,
        "source_sha256": _sha256(path.read_bytes()),
        "language": case["language"],
        "language_mode": case["language_mode"],
        "languages": list(case["languages"]),
        "routing_policy": case["routing_policy"],
    }


def _run_case(case: dict[str, Any], engine_name: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with _selected_engine(engine_name):
            result = execute_ocr_text(
                case["path"],
                language=case["language"],
                language_mode=case["language_mode"],
                languages=case["languages"],
                routing_policy=case["routing_policy"],
            )
        return {
            "engine": engine_name,
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "summary": _result_summary(result),
        }
    except Exception as exc:  # evidence records type only; document text is never stored
        return {
            "engine": engine_name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error_type": type(exc).__name__,
        }


def _environment_metadata() -> dict[str, Any]:
    import platen_document
    from platen_document import DocumentProcessor

    doctor = DocumentProcessor().capabilities()
    try:
        worker_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT / "pdfnest-worker",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        worker_revision = "unavailable"
    return {
        "schema_version": "pdfnest_ocr_text_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "worker_revision": worker_revision,
        "boundary_module": str(Path(__file__).resolve()),
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; no PDFNest services; no managed resources",
        "ocr_text_engine_default": "internal",
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fixtures: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for case in CASES:
        if not Path(case["path"]).is_file():
            raise FileNotFoundError(f"parity fixture is missing: {case['name']}")
        fixtures.append(_fixture_metadata(case))
        internal = _run_case(case, "internal")
        sdk = _run_case(case, "sdk")
        matched = bool(internal.get("ok") and sdk.get("ok") and internal["summary"] == sdk["summary"])
        cases.append(
            {
                "fixture": fixtures[-1],
                "internal": internal,
                "sdk": sdk,
                "parity": "MATCH" if matched else "MISMATCH",
            }
        )

    summary = {
        "schema_version": "pdfnest_ocr_text_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; direct worker consumer boundary; no PDFNest services",
        "engines": ["internal", "sdk"],
        "case_count": len(cases),
        "matched_cases": sum(item["parity"] == "MATCH" for item in cases),
        "mismatched_cases": sum(item["parity"] != "MATCH" for item in cases),
        "cases": cases,
    }
    (OUTPUT_ROOT / "fixture-inventory.json").write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "parity-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "run-metadata.json").write_text(json.dumps(_environment_metadata(), indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases")}, sort_keys=True))
    return 0 if summary["mismatched_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

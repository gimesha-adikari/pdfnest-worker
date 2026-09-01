"""Run the General Editor OCR V2 internal-versus-SDK parity gate.

This is a PDFNest consumer-boundary check.  It deliberately compares the
editor layout projection produced by the two selectable implementations rather
than comparing the standalone SDK to itself.  Document text is represented by
length/hash summaries in persisted evidence.

The current General Editor V2 contract calls OCR with ``language="eng"`` and
does not expose explicit language, multilingual, or AUTO controls.  The
Sinhala and bilingual fixtures below therefore exercise that unchanged
English-contract behavior; they are not claims that the Editor currently
offers a language picker.
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

from app.core.editor_ocr_engine import execute_editor_ocr


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "document-sdk-general-editor-consumer-01"


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "native_english",
        "kind": "repository-approved-native-pdf",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "scanned_english",
        "kind": "approved-real-derived-scanned-english",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-english.pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "sinhala_fixture_current_english_contract",
        "kind": "approved-real-derived-sinhala-current-editor-contract",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-sinhala.pdf",
        "language_contract": "eng-fixed-editor-contract; explicit Sinhala unsupported",
    },
    {
        "name": "bilingual_fixture_current_english_contract",
        "kind": "approved-real-derived-bilingual-current-editor-contract",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-bilingual.pdf",
        "language_contract": "eng-fixed-editor-contract; multilingual unsupported",
    },
    {
        "name": "mixed_native_scanned",
        "kind": "approved-real-derived-native-plus-scanned",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-mixed-native-bilingual.pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "rotated_cropbox",
        "kind": "approved-real-scanned-rotation-fixture",
        "path": APPROVED_ROOT / "ocr-extracted-text-29-rotated (1).pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "rotated_editor_regression",
        "kind": "approved-real-editor-rotation-fixture",
        "path": APPROVED_ROOT / "CV-1-rotated.pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "editor_real_document_control",
        "kind": "approved-real-editor-document-control",
        "path": APPROVED_ROOT / "document-studio (2).pdf",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "malformed_input",
        "kind": "generated-malformed-input-control",
        "control": "malformed",
        "language_contract": "eng-fixed-editor-contract",
    },
    {
        "name": "cancellation_before_start",
        "kind": "generated-cancellation-control",
        "control": "cancel-before-start",
        "language_contract": "eng-fixed-editor-contract",
    },
)


class _CancelProbe(RuntimeError):
    """Private control exception used only to compare cancellation behavior."""


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _safe_projection(value: Any, key: str | None = None) -> Any:
    """Preserve semantic editor fields without persisting document text."""

    if key in {"text", "original_text"} and isinstance(value, str):
        return {"length": len(value), "sha256": _sha256(value)}
    if key == "source_id":
        return "fixture-source"
    if isinstance(value, dict):
        return {
            child_key: _safe_projection(child_value, child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_projection(item, key) for item in value]
    if isinstance(value, float):
        return round(value, 4)
    return _enum_value(value)


def _editor_summary(result: dict[str, Any]) -> dict[str, Any]:
    return _safe_projection(result)


def _differing_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
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
        paths: list[str] = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            paths.extend(_differing_paths(left_item, right_item, f"{prefix}[{index}]"))
        return paths
    return [] if left == right else [prefix or "$"]


@contextmanager
def _selected_engine(name: str) -> Iterator[None]:
    previous = os.environ.get("EDITOR_OCR_ENGINE")
    os.environ["EDITOR_OCR_ENGINE"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("EDITOR_OCR_ENGINE", None)
        else:
            os.environ["EDITOR_OCR_ENGINE"] = previous


def _path_label(path: Path) -> str:
    if path.is_relative_to(APPROVED_ROOT):
        return f"approved-real-document/{path.relative_to(APPROVED_ROOT)}"
    if path.is_relative_to(ROOT):
        return str(path.relative_to(ROOT))
    return f"generated-control/{path.name}"


def _fixture_metadata(case: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "name": case["name"],
        "kind": case["kind"],
        "path_label": _path_label(path),
        "source_bytes": path.stat().st_size,
        "source_sha256": _sha256(path.read_bytes()),
        "language_contract": case["language_contract"],
        "editor_language_support": {
            "fixed_language": "eng",
            "explicit_sinhala": False,
            "explicit_multilingual": False,
            "auto": False,
        },
    }


def _make_control(case: dict[str, Any], temporary: Path) -> tuple[Path, dict[str, Any] | None]:
    control = case.get("control")
    if control == "malformed":
        path = temporary / "malformed-editor-input.pdf"
        path.write_bytes(b"not a PDF")
        return path, None
    if control == "cancel-before-start":
        path = temporary / "cancel-editor-input.pdf"
        document = fitz.open()
        document.new_page(width=300, height=200).insert_text((30, 80), "Cancellation control", fontsize=20)
        document.save(path)
        document.close()
        return path, {"cancel_before_start": True}
    path = Path(case["path"])
    return path, None


def _run_case(case: dict[str, Any], engine_name: str, path: Path, control: dict[str, Any] | None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        cancellation_check = None
        if control and control.get("cancel_before_start"):
            def cancellation_check() -> None:
                raise _CancelProbe("cancel control")

        with _selected_engine(engine_name):
            result = execute_editor_ocr(
                path,
                cancellation_check=cancellation_check,
            )
        return {
            "engine": engine_name,
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "summary": _editor_summary(result),
        }
    except Exception as exc:  # only safe type/classification metadata is persisted
        return {
            "engine": engine_name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": {
                "type": type(exc).__name__,
                "module": type(exc).__module__,
                "classification": getattr(exc, "reason_code", None),
                "stage": getattr(exc, "substage", None),
            },
        }


def _environment_metadata() -> dict[str, Any]:
    import importlib.metadata
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
    try:
        sdk_version = importlib.metadata.version("platen-document")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "unavailable"
    return {
        "schema_version": "pdfnest_general_editor_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "worker_revision": revisions["worker"],
        "sdk_revision": revisions["sdk"],
        "sdk_version": sdk_version,
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; direct General Editor consumer boundary; no PDFNest services; no managed resources",
        "editor_ocr_engine_default": "internal",
        "editor_language_contract": {
            "fixed_language": "eng",
            "explicit_language_selection": False,
            "multilingual": False,
            "auto": False,
        },
    }


def _write_secondary_summaries(cases: list[dict[str, Any]]) -> None:
    def write(name: str, selected: list[dict[str, Any]]) -> None:
        payload = {
            "schema_version": f"pdfnest_general_editor_{name.replace('-', '_')}.v1",
            "cases": selected,
            "matched_cases": sum(item["parity"] == "MATCH" for item in selected),
            "case_count": len(selected),
        }
        (OUTPUT_ROOT / f"{name}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )

    write("native-parity", [item for item in cases if item["fixture"]["name"] == "native_english"])
    write(
        "scanned-parity",
        [
            item
            for item in cases
            if "scanned" in item["fixture"]["name"] or "sinhala" in item["fixture"]["name"]
        ],
    )
    write(
        "multilingual-parity",
        [
            item
            for item in cases
            if "bilingual" in item["fixture"]["name"] or "sinhala" in item["fixture"]["name"]
        ],
    )
    write(
        "rotation-geometry",
        [item for item in cases if "rotat" in item["fixture"]["name"] or "cropbox" in item["fixture"]["name"]],
    )
    write("editor-projection-parity", cases)
    write(
        "failure-parity",
        [item for item in cases if item["fixture"]["name"] in {"malformed_input", "cancellation_before_start"}],
    )


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fixture_inventory: list[dict[str, Any]] = []
    parity_cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pdfnest-editor-parity-") as temporary_name:
        temporary = Path(temporary_name)
        for case in CASES:
            path, control = _make_control(case, temporary)
            if not path.is_file():
                raise FileNotFoundError(f"parity fixture is missing: {case['name']}")
            fixture = _fixture_metadata(case, path)
            if control:
                fixture["control"] = control
            fixture_inventory.append(fixture)
            internal = _run_case(case, "internal", path, control)
            sdk = _run_case(case, "sdk", path, control)
            if internal["ok"] and sdk["ok"]:
                differing = _differing_paths(internal["summary"], sdk["summary"])
                matched = not differing
                material = bool(differing)
            elif not internal["ok"] and not sdk["ok"]:
                differing = _differing_paths(internal["error"], sdk["error"])
                matched = not differing
                material = bool(differing)
            else:
                differing = ["success/failure"]
                matched = False
                material = True
            parity_cases.append(
                {
                    "fixture": fixture,
                    "internal": internal,
                    "sdk": sdk,
                    "parity": "MATCH" if matched else "MISMATCH",
                    "differing_fields": differing,
                    "material": material,
                    "resolution": "none; exact semantic match" if matched else "investigate before promotion",
                }
            )

    summary = {
        "schema_version": "pdfnest_general_editor_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; General Editor selector boundary; no PDFNest services; no managed resources",
        "engines": ["internal", "sdk"],
        "case_count": len(parity_cases),
        "matched_cases": sum(item["parity"] == "MATCH" for item in parity_cases),
        "mismatched_cases": sum(item["parity"] != "MATCH" for item in parity_cases),
        "material_mismatches": sum(item["material"] for item in parity_cases),
        "editor_language_contract": {
            "fixed_language": "eng",
            "explicit_sinhala_supported": False,
            "explicit_multilingual_supported": False,
            "auto_supported": False,
        },
        "cases": parity_cases,
    }
    (OUTPUT_ROOT / "fixture-inventory.json").write_text(
        json.dumps(fixture_inventory, indent=2, ensure_ascii=False) + "\n"
    )
    (OUTPUT_ROOT / "parity-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )
    (OUTPUT_ROOT / "run-metadata.json").write_text(
        json.dumps(_environment_metadata(), indent=2, ensure_ascii=False) + "\n"
    )
    _write_secondary_summaries(parity_cases)
    print(
        json.dumps(
            {key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases", "material_mismatches")},
            sort_keys=True,
        )
    )
    return 0 if summary["mismatched_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

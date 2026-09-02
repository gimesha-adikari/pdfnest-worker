"""Run the legacy PDF Editor internal-versus-SDK consumer parity gate.

This harness compares the ordinary /edit-pdf consumer contract. The internal
side is the unchanged historical extractor; the SDK side is the new
legacy-editor selector and its compatibility projection. Initial analysis can
also capture the unprojected public SDK result so that compatibility seams are
not mistaken for OCR parity.

Only safe summaries are persisted. Document text is represented by lengths and
SHA-256 digests, never by full content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pymupdf as fitz

from app.api.tools.editor.document import extract_document
from app.core.editor_ocr_projection import project_editor_result
from app.core.legacy_editor_ocr_engine import (
    LEGACY_EDITOR_OCR_ENGINE_ENV,
    execute_legacy_editor_ocr,
)
from app.jobs.cancellation import JobCancelledException


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "document-sdk-legacy-editor-consumer-01"

CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "native_english",
        "kind": "repository-approved-native-pdf",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "normal_text.pdf",
        "contract": "native-first legacy Editor extraction",
    },
    {
        "name": "scanned_english",
        "kind": "approved-real-derived-scanned-english",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-english.pdf",
        "contract": "legacy fixed eng OCR fallback",
    },
    {
        "name": "multi_page_scanned",
        "kind": "repository-approved-real-three-page-image-pdf",
        "path": APPROVED_ROOT / "compiled-images.pdf",
        "contract": "legacy fixed eng OCR fallback",
    },
    {
        "name": "mixed_native_scanned",
        "kind": "approved-real-derived-native-plus-scanned",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-mixed-native-bilingual.pdf",
        "contract": "native-first per-page routing; fixed eng OCR fallback",
    },
    {
        "name": "password_protected",
        "kind": "repository-approved-password-control",
        "path": ROOT / "pdfnest" / "tests" / "fixtures" / "locked_sample.pdf",
        "contract": "password is accepted by the current Editor contract",
        "password_env": "PDFNEST_EDITOR_PARITY_FIXTURE_PASSWORD",
    },
    {
        "name": "rotated_cropbox",
        "kind": "approved-real-rotated-structured-pdf",
        "path": APPROVED_ROOT / "ocr-extracted-text-29-rotated (1).pdf",
        "contract": "legacy page geometry and ordering",
    },
    {
        "name": "repeated_text_reading_order",
        "kind": "approved-real-editor-layout-control",
        "path": APPROVED_ROOT / "chatgpt.com_layout.pdf",
        "contract": "native text order and repeated-content control",
    },
    {
        "name": "fixed_language_sinhala",
        "kind": "approved-real-sinhala-current-editor-contract",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-sinhala.pdf",
        "contract": "fixed eng legacy contract; no language selector",
    },
    {
        "name": "fixed_language_bilingual",
        "kind": "approved-real-bilingual-current-editor-contract",
        "path": ROOT / "platen-document" / "output" / "extraction-parity-01" / "inputs" / "approved-bilingual.pdf",
        "contract": "fixed eng legacy contract; multilingual selector unsupported",
    },
    {
        "name": "native_table_control",
        "kind": "approved-real-native-document-control",
        "path": APPROVED_ROOT / "ICT_1000_Full_MCQs (2).pdf",
        "contract": "native Editor layout control with table-like content",
    },
    {
        "name": "malformed_input",
        "kind": "generated-malformed-input-control",
        "control": "malformed",
        "contract": "controlled invalid input failure",
    },
    {
        "name": "cancellation_before_start",
        "kind": "generated-cancellation-control",
        "control": "cancel-before-start",
        "contract": "cooperative cancellation before processing",
    },
)


SCANNED_DPI_DIVERGENCE = (
    "The legacy path gives Tesseract a PNG without DPI metadata; the public "
    "SDK raster path embeds 144-DPI metadata. A no-DPI control reproduced the "
    "legacy token output, while the SDK PNG produced a different token stream."
)

CASE_ASSESSMENTS: dict[str, dict[str, Any]] = {
    "mixed_native_scanned": {
        "classification": "REAL_PRODUCT_DIVERGENCE",
        "material": True,
        "cause": SCANNED_DPI_DIVERGENCE,
        "resolution": "unresolved; an SDK raster/preprocessing change is out of scope",
    },
    "fixed_language_sinhala": {
        "classification": "REAL_PRODUCT_DIVERGENCE",
        "material": True,
        "cause": SCANNED_DPI_DIVERGENCE,
        "resolution": "unresolved; an SDK raster/preprocessing change is out of scope",
    },
    "fixed_language_bilingual": {
        "classification": "REAL_PRODUCT_DIVERGENCE",
        "material": True,
        "cause": SCANNED_DPI_DIVERGENCE,
        "resolution": "unresolved; an SDK raster/preprocessing change is out of scope",
    },
}


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _digest(text: str) -> dict[str, Any]:
    return {"length": len(text), "sha256": _sha256(text)}


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    return value


def _element_summary(element: dict[str, Any]) -> dict[str, Any]:
    text = str(element.get("text", ""))
    return {
        "text": _digest(text),
        "geometry": {
            key: _rounded(element.get(key))
            for key in ("x", "y", "width", "height")
        },
        "size": _rounded(element.get("size")),
    }


def _page_summary(page: dict[str, Any]) -> dict[str, Any]:
    elements = [
        _element_summary(element)
        for element in page.get("elements", [])
        if isinstance(element, dict)
    ]
    text_digest = _digest("\n".join(
        str(element.get("text", ""))
        for element in page.get("elements", [])
        if isinstance(element, dict)
    ))
    return {
        "page_num": page.get("page_num"),
        "width": _rounded(page.get("width")),
        "height": _rounded(page.get("height")),
        "kind": page.get("kind"),
        "has_selectable_text": page.get("has_selectable_text"),
        "word_count": page.get("word_count"),
        "text_block_count": page.get("text_block_count"),
        "image_block_count": page.get("image_block_count"),
        "element_count": len(elements),
        "text": text_digest,
        "elements": elements,
    }


def _safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": result.get("success"),
        "pages": [_page_summary(page) for page in result.get("pages", [])],
    }


def _safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "reason_code": getattr(exc, "reason_code", None),
        "stage": getattr(exc, "substage", None),
    }


def _diff(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "$"]
    if isinstance(left, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_diff(left[key], right[key], child))
        return paths
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix or "$"]
        paths: list[str] = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            paths.extend(_diff(left_item, right_item, f"{prefix}[{index}]"))
        return paths
    return [] if left == right else [prefix or "$"]


def _matching_top_level_fields(left: Any, right: Any) -> list[str]:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return []
    return [
        key
        for key in sorted(set(left) & set(right))
        if not _diff(left[key], right[key])
    ]


def _path_label(path: Path) -> str:
    if path.is_relative_to(APPROVED_ROOT):
        return f"approved-real-document/{path.relative_to(APPROVED_ROOT)}"
    if path.is_relative_to(ROOT):
        return str(path.relative_to(ROOT))
    return f"generated-control/{path.name}"


def _make_control(case: dict[str, Any], temporary: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
    control = case.get("control")
    if control == "malformed":
        path = temporary / "malformed-editor-input.pdf"
        path.write_bytes(b"not a PDF")
        return path, {"name": "malformed"}, None
    if control == "cancel-before-start":
        path = temporary / "cancel-editor-input.pdf"
        document = fitz.open()
        document.new_page(width=300, height=200).insert_text(
            (30, 80),
            "Cancellation control",
            fontsize=20,
        )
        document.save(path)
        document.close()
        return path, {"name": "cancel-before-start"}, None
    password = None
    password_env = case.get("password_env")
    if password_env:
        password = os.getenv(password_env)
    return Path(case["path"]), None, password


def _fixture_metadata(case: dict[str, Any], path: Path, control: dict[str, Any] | None) -> dict[str, Any]:
    page_count: int | None = None
    page_sizes: list[list[Any]] = []
    if not control or control.get("name") != "malformed":
        try:
            with fitz.open(path) as document:
                password_env = case.get("password_env")
                password = os.getenv(password_env) if password_env else None
                if document.needs_pass and password:
                    document.authenticate(password)
                page_count = len(document)
                page_sizes = [
                    [_rounded(page.rect.width), _rounded(page.rect.height)]
                    for page in list(document)[:8]
                ]
        except (fitz.FileDataError, RuntimeError):
            page_count = None
    result = {
        "name": case["name"],
        "kind": case["kind"],
        "path_label": _path_label(path),
        "source_bytes": path.stat().st_size,
        "source_sha256": _sha256(path.read_bytes()),
        "contract": case["contract"],
        "page_count": page_count,
        "page_sizes_sample": page_sizes,
        "language_contract": {
            "fixed_language": "eng",
            "explicit_language_selection": False,
            "multilingual": False,
            "auto": False,
        },
    }
    if control:
        result["control"] = control
    return result


def _run_internal(path: Path, password: str | None, control: dict[str, Any] | None) -> dict[str, Any]:
    try:
        cancellation_check = None
        if control and control["name"] == "cancel-before-start":
            cancellation_check = lambda: (_ for _ in ()).throw(
                JobCancelledException("cancelled by parity control")
            )
        result = extract_document(
            str(path),
            password,
            cancellation_check=cancellation_check,
        )
        return {"ok": True, "summary": _safe_result_summary(result)}
    except Exception as exc:
        return {"ok": False, "error": _safe_error(exc)}


def _run_selected(path: Path, password: str | None, control: dict[str, Any] | None, engine_name: str) -> dict[str, Any]:
    previous = os.environ.get(LEGACY_EDITOR_OCR_ENGINE_ENV)
    os.environ[LEGACY_EDITOR_OCR_ENGINE_ENV] = engine_name
    try:
        cancellation_check = None
        if control and control["name"] == "cancel-before-start":
            cancellation_check = lambda: (_ for _ in ()).throw(
                JobCancelledException("cancelled by parity control")
            )
        result = execute_legacy_editor_ocr(
            path,
            password,
            cancellation_check=cancellation_check,
        )
        return {"ok": True, "summary": _safe_result_summary(result)}
    except Exception as exc:
        return {"ok": False, "error": _safe_error(exc)}
    finally:
        if previous is None:
            os.environ.pop(LEGACY_EDITOR_OCR_ENGINE_ENV, None)
        else:
            os.environ[LEGACY_EDITOR_OCR_ENGINE_ENV] = previous


def _run_raw_sdk(path: Path, password: str | None, control: dict[str, Any] | None) -> dict[str, Any]:
    try:
        if control and control["name"] == "cancel-before-start":
            raise JobCancelledException("cancelled by parity control")
        from platen_document import DocumentProcessor, EngineConfiguration, OCRProfile

        processor = DocumentProcessor(
            EngineConfiguration(
                max_raster_pixels=25_000_000,
                raster_dpi=144,
            )
        )
        result = processor.extract_text(
            path,
            password=password,
            language="eng",
            profile=OCRProfile.OCR_TEXT_V2,
            routing_policy="FAST",
        )
        return {"ok": True, "summary": _safe_result_summary(project_editor_result(result))}
    except Exception as exc:
        return {"ok": False, "error": _safe_error(exc)}


def _parity_record(fixture: dict[str, Any], internal: dict[str, Any], sdk: dict[str, Any]) -> dict[str, Any]:
    assessment = CASE_ASSESSMENTS.get(fixture["name"], {})
    if internal["ok"] and sdk["ok"]:
        differing = _diff(internal["summary"], sdk["summary"])
        matched = not differing
        matched_fields = _matching_top_level_fields(internal["summary"], sdk["summary"])
    elif not internal["ok"] and not sdk["ok"]:
        differing = _diff(internal["error"], sdk["error"])
        matched = not differing
        matched_fields = _matching_top_level_fields(internal["error"], sdk["error"])
    else:
        differing = ["success/failure"]
        matched = False
        matched_fields = []
    if matched:
        classification = "MATCH"
        material = False
        cause = "none; exact semantic legacy contract match"
        resolution = "compatibility projection and legacy native-first routing verified"
    else:
        classification = str(assessment.get("classification", "UNKNOWN"))
        material = bool(assessment.get("material", True))
        cause = str(assessment.get("cause", "unclassified difference; investigate before promotion"))
        resolution = str(assessment.get("resolution", "unresolved"))
    return {
        "fixture": fixture,
        "internal": internal,
        "sdk": sdk,
        "parity": "MATCH" if matched else "MISMATCH",
        "matched_fields": matched_fields,
        "differing_fields": differing,
        "mismatch_classification": classification,
        "material": material,
        "cause": cause,
        "resolution": resolution,
    }


def _environment() -> dict[str, Any]:
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
        "schema_version": "pdfnest_legacy_editor_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "worker_revision": revisions["worker"],
        "sdk_revision": revisions["sdk"],
        "sdk_version": sdk_version,
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; legacy_editor consumer boundary; no PDFNest services; no managed resources",
        "legacy_editor_ocr_engine_default": "internal",
        "legacy_editor_language_contract": {
            "fixed_language": "eng",
            "explicit_language_selection": False,
            "multilingual": False,
            "auto": False,
        },
    }


def run_initial_analysis() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdfnest-legacy-editor-parity-") as temporary_name:
        temporary = Path(temporary_name)
        for case in CASES:
            path, control, password = _make_control(case, temporary)
            if not path.is_file():
                missing.append(case["name"])
                continue
            fixture = _fixture_metadata(case, path, control)
            internal = _run_internal(path, password, control)
            raw_sdk = _run_raw_sdk(path, password, control)
            if internal["ok"] and raw_sdk["ok"]:
                differing = _diff(internal["summary"], raw_sdk["summary"])
            elif not internal["ok"] and not raw_sdk["ok"]:
                differing = _diff(internal["error"], raw_sdk["error"])
            else:
                differing = ["success/failure"]
            records.append(
                {
                    "fixture": fixture,
                    "internal": internal,
                    "unprojected_sdk": raw_sdk,
                    "differing_fields": differing,
                    "classification": "NO_INITIAL_NORMALIZATION",
                    "material": bool(differing),
                    "cause": "captured before legacy compatibility projection",
                }
            )
    payload = {
        "schema_version": "pdfnest_legacy_editor_initial_parity_analysis.v1",
        "comparison": "historical legacy projection versus public SDK projection before compatibility mapping",
        "cases": records,
        "case_count": len(records),
        "missing_cases": missing,
        "material_mismatch_count": sum(item["material"] for item in records),
    }
    (OUTPUT_ROOT / "initial-parity-analysis.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps({"case_count": len(records), "material_mismatch_count": payload["material_mismatch_count"], "missing_cases": missing}, sort_keys=True))
    return 0 if not missing else 1


def run_final_parity() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    missing: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdfnest-legacy-editor-parity-") as temporary_name:
        temporary = Path(temporary_name)
        for case in CASES:
            path, control, password = _make_control(case, temporary)
            if not path.is_file():
                missing.append(case["name"])
                continue
            fixture = _fixture_metadata(case, path, control)
            fixtures.append(fixture)
            internal = _run_internal(path, password, control)
            sdk = _run_selected(path, password, control, "sdk")
            records.append(_parity_record(fixture, internal, sdk))

    summary = {
        "schema_version": "pdfnest_legacy_editor_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; legacy_editor selector boundary; no PDFNest services; no managed resources",
        "engines": ["internal", "sdk"],
        "case_count": len(records),
        "matched_cases": sum(item["parity"] == "MATCH" for item in records),
        "mismatched_cases": sum(item["parity"] != "MATCH" for item in records),
        "material_mismatches": sum(item["material"] for item in records),
        "missing_cases": missing,
        "legacy_editor_language_contract": {
            "fixed_language": "eng",
            "explicit_sinhala_supported": False,
            "explicit_multilingual_supported": False,
            "auto_supported": False,
        },
        "cases": records,
    }
    (OUTPUT_ROOT / "fixture-inventory.json").write_text(
        json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n"
    )
    (OUTPUT_ROOT / "parity-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )
    (OUTPUT_ROOT / "run-metadata.json").write_text(
        json.dumps(_environment(), indent=2, ensure_ascii=False) + "\n"
    )
    for name, selected in {
        "native-parity": [item for item in records if item["fixture"]["name"] in {"native_english", "native_table_control", "repeated_text_reading_order"}],
        "scanned-parity": [item for item in records if "scanned" in item["fixture"]["name"] or "sinhala" in item["fixture"]["name"]],
        "mixed-parity": [item for item in records if item["fixture"]["name"] == "mixed_native_scanned"],
        "geometry-parity": [item for item in records if item["fixture"]["name"] == "rotated_cropbox"],
        "error-parity": [item for item in records if item["fixture"]["name"] in {"malformed_input", "cancellation_before_start", "password_protected"}],
    }.items():
        (OUTPUT_ROOT / f"{name}.json").write_text(
            json.dumps(
                {
                    "schema_version": f"pdfnest_legacy_editor_{name.replace('-', '_')}.v1",
                    "cases": selected,
                    "case_count": len(selected),
                    "matched_cases": sum(item["parity"] == "MATCH" for item in selected),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    print(json.dumps({key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases", "material_mismatches", "missing_cases")}, sort_keys=True))
    return 0 if not missing and summary["mismatched_cases"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--initial-only",
        action="store_true",
        help="capture the pre-compatibility public SDK differential",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="write evidence to this directory instead of the historical default",
    )
    args = parser.parse_args()
    if args.output_root is not None:
        global OUTPUT_ROOT
        OUTPUT_ROOT = args.output_root.resolve()
    return run_initial_analysis() if args.initial_only else run_final_parity()


if __name__ == "__main__":
    raise SystemExit(main())

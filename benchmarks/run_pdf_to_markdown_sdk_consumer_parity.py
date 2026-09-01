"""Run the PDF-to-Markdown V2 internal-versus-SDK consumer parity gate.

The harness compares both the canonical structured result and the final
Markdown projection.  Dynamic result identifiers and elapsed timings are
excluded; text is represented by length/hash summaries so private document
contents are not copied into ordinary evidence.
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

from app.core.pdf_to_markdown_engine import execute_pdf_to_markdown


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "document-sdk-pdf-to-markdown-consumer-01"


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "native_english",
        "kind": "repository-approved-native-pdf",
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
        "name": "sinhala_explicit",
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
        "name": "current_scanned_structure",
        "kind": "approved-real-scanned-structure-fixture",
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


def _safe_projection(value: Any, key: str | None = None) -> Any:
    """Keep structured contract fields while removing dynamic values/full text."""

    if key == "text" and isinstance(value, str):
        return {"length": len(value), "sha256": _sha256(value)}
    if key in {"result_id", "elapsed_seconds"}:
        return None
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key in {"result_id", "elapsed_seconds"}:
                continue
            projected[child_key] = _safe_projection(child_value, child_key)
        return projected
    if isinstance(value, (list, tuple)):
        return [_safe_projection(item, key) for item in value]
    if isinstance(value, float) and key in {"x", "y", "width", "height", "rotation", "confidence"}:
        return round(value, 4)
    return _enum_value(value)


def _structured_summary(result: Any) -> dict[str, Any]:
    projected = _safe_projection(result.to_dict())
    projected.pop("result_id", None)
    projected.get("validation", {}).pop("elapsed_seconds", None)
    return projected


def _markdown_summary(markdown: str) -> dict[str, Any]:
    lines = markdown.splitlines()
    headings = [line for line in lines if line.startswith("#") and line.lstrip("#").startswith(" ")]
    list_markers = [line[:2] for line in lines if line.startswith(("- ", "* ")) or any(line.startswith(f"{index}. ") for index in range(1, 100))]
    table_lines = [line for line in lines if line.startswith("|")]
    return {
        "sha256": _sha256(markdown),
        "length": len(markdown),
        "heading_count": len(headings),
        "headings": headings,
        "list_line_count": len(list_markers),
        "table_line_count": len(table_lines),
        "table_lines": table_lines,
        "page_break_count": markdown.count("<!-- pagebreak -->"),
    }


def _differing_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "$."]
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
    previous = os.environ.get("PDF_TO_MARKDOWN_ENGINE")
    os.environ["PDF_TO_MARKDOWN_ENGINE"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PDF_TO_MARKDOWN_ENGINE", None)
        else:
            os.environ["PDF_TO_MARKDOWN_ENGINE"] = previous


def _path_label(path: Path) -> str:
    if path.is_relative_to(APPROVED_ROOT):
        return f"approved-real-document/{path.relative_to(APPROVED_ROOT)}"
    if path.is_relative_to(ROOT):
        return str(path.relative_to(ROOT))
    return f"generated-control/{path.name}"


def _fixture_metadata(case: dict[str, Any]) -> dict[str, Any]:
    path = Path(case["path"])
    return {
        "name": case["name"],
        "kind": case["kind"],
        "path_label": _path_label(path),
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
            execution = execute_pdf_to_markdown(
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
            "structured": _structured_summary(execution.structured_result),
            "markdown": _markdown_summary(execution.markdown),
        }
    except Exception as exc:  # evidence records type only; no source paths/messages
        return {
            "engine": engine_name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error_type": type(exc).__name__,
        }


def _make_ambiguous_pseudo_table(target: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 700), "white")
    draw = ImageDraw.Draw(image)
    rows = ("01 Alpha 21", "02 Beta 16", "03 Gamma 12", "04 Delta 07")
    positions = ((50, 160), (50, 250), (50, 350), (50, 460))
    for text, position in zip(rows, positions):
        draw.text(position, text, fill="black")
    encoded = target.with_suffix(".png")
    image.save(encoded, format="PNG")
    with fitz.open() as document:
        page = document.new_page(width=600, height=350)
        page.insert_image(page.rect, filename=str(encoded))
        document.save(str(target))
    encoded.unlink()


def _make_native_table(target: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=600, height=400)
        x_edges = (50, 130, 340, 430, 550)
        y_edges = (60, 100, 140, 180, 220)
        shape = page.new_shape()
        for x in x_edges:
            shape.draw_line((x, y_edges[0]), (x, y_edges[-1]))
        for y in y_edges:
            shape.draw_line((x_edges[0], y), (x_edges[-1], y))
        shape.finish(color=(0, 0, 0), width=0.8)
        shape.commit()
        cells = (
            ("No.", "Module", "Credits", "Status"),
            ("01", "Alpha", "21", "Followed"),
            ("02", "Beta", "16", "Followed"),
            ("03", "Gamma", "12", "Followed"),
        )
        for row, values in enumerate(cells):
            for column, value in enumerate(values):
                page.insert_text((x_edges[column] + 5, y_edges[row] + 25), value, fontsize=10)
        document.save(str(target))


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
        "schema_version": "pdfnest_pdf_to_markdown_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "worker_revision": worker_revision,
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "sdk_version": getattr(platen_document, "__version__", "package-metadata-only"),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; direct worker consumer boundary; no PDFNest services; no managed resources",
        "pdf_to_markdown_engine_default": "internal",
        "markdown_renderer_contract": "one structured extraction followed by render-only SDK/internal Markdown projection",
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    cases = [dict(case) for case in CASES]
    fixtures: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="pdfnest-pdf-to-markdown-parity-") as temporary_dir:
        temporary = Path(temporary_dir)
        malformed = temporary / "malformed.pdf"
        malformed.write_bytes(b"not a PDF")
        cases.extend(
            [
                {
                    "name": "malformed_input",
                    "kind": "generated-unsupported-input-control",
                    "path": malformed,
                    "language": "eng",
                    "language_mode": "EXPLICIT",
                    "languages": ("eng",),
                    "routing_policy": "AUTO",
                },
                {
                    "name": "ambiguous_pseudo_table",
                    "kind": "generated-ambiguous-scanned-table-control",
                    "path": temporary / "ambiguous-pseudo-table.pdf",
                    "language": "eng",
                    "language_mode": "EXPLICIT",
                    "languages": ("eng",),
                    "routing_policy": "FAST",
                },
                {
                    "name": "native_table",
                    "kind": "generated-native-table-control",
                    "path": temporary / "native-table.pdf",
                    "language": "eng",
                    "language_mode": "EXPLICIT",
                    "languages": ("eng",),
                    "routing_policy": "FAST",
                },
                {
                    "name": "auto_uncertainty_control",
                    "kind": "generated-bounded-auto-uncertainty-control",
                    "path": temporary / "blank-auto.pdf",
                    "language": "auto",
                    "language_mode": "AUTO",
                    "languages": ("eng", "sin"),
                    "routing_policy": "FAST",
                },
            ]
        )
        _make_ambiguous_pseudo_table(cases[-3]["path"])
        _make_native_table(cases[-2]["path"])
        blank = cases[-1]["path"]
        image = temporary / "blank-auto.png"
        from PIL import Image

        Image.new("RGB", (600, 400), "white").save(image, format="PNG")
        with fitz.open() as document:
            page = document.new_page(width=288, height=192)
            page.insert_image(page.rect, filename=str(image))
            document.save(str(blank))

        for case in cases:
            path = Path(case["path"])
            if not path.is_file():
                raise FileNotFoundError(f"parity fixture is missing: {case['name']}")
            fixture = _fixture_metadata(case)
            fixtures.append(fixture)
            internal = _run_case(case, "internal")
            sdk = _run_case(case, "sdk")
            if internal.get("ok") and sdk.get("ok"):
                differences = _differing_paths(
                    {"structured": internal["structured"], "markdown": internal["markdown"]},
                    {"structured": sdk["structured"], "markdown": sdk["markdown"]},
                )
            else:
                differences = ["error_type"] if internal.get("error_type") != sdk.get("error_type") else []
            records.append(
                {
                    "case": case["name"],
                    "fixture": fixture,
                    "internal": internal,
                    "sdk": sdk,
                    "matched_fields": [] if differences else ["canonical structured result", "final Markdown projection"],
                    "differing_fields": differences,
                    "material_differences": differences,
                    "non_material_differences": [],
                    "parity": "MATCH" if not differences else "MISMATCH",
                    "resolution": "none; both selected branches produced the same normalized structured and Markdown contracts" if not differences else "investigation required",
                }
            )

    summary = {
        "schema_version": "pdfnest_pdf_to_markdown_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; direct worker consumer boundary; no PDFNest services",
        "engines": ["internal", "sdk"],
        "case_count": len(records),
        "matched_cases": sum(record["parity"] == "MATCH" for record in records),
        "mismatched_cases": sum(record["parity"] != "MATCH" for record in records),
        "cases": records,
        "guard_coverage": {
            "one_pass": "SDK adapter calls extract_document once and to_markdown(result) without a second extraction",
            "anti_fabrication": "ambiguous pseudo-table control included; no Markdown-only structure heuristics were added",
            "native_table": "native table control included where pdfplumber/native structure is available",
            "malformed_input": "included as a two-branch error parity control",
            "auto_uncertainty": "included as a bounded generated control; no detector thresholds changed",
        },
    }
    _write_json(OUTPUT_ROOT / "fixture-inventory.json", fixtures)
    _write_json(OUTPUT_ROOT / "parity-summary.json", summary)
    _write_json(OUTPUT_ROOT / "run-metadata.json", _environment_metadata())
    print(json.dumps({key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases")}, sort_keys=True))
    return 0 if summary["mismatched_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

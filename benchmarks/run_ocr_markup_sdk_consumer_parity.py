"""Run the OCR-aware markup family internal-versus-SDK parity gate.

The harness exercises the three standalone V2 products through the
consumer-specific selector and records contract-safe summaries.  It does not
persist OCR document text; target text is represented by a test-case label
and hash, while annotation geometry, result metadata, and PDF semantics are
compared directly.
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
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_markup_engine import execute_ocr_markup


ROOT = Path(__file__).resolve().parents[2]
APPROVED_ROOT = Path("/home/gimesha/pdfnest-tests")
OUTPUT_ROOT = ROOT / "output" / "document-sdk-markup-consumers-01"


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _make_native_pdf(path: Path, *, repeated: bool = False) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    if repeated:
        lines = ("Alpha Bravo", "Alpha in a second occurrence", "Alpha Bravo again")
    else:
        lines = ("Markup Alpha Bravo", "Native English fixture")
    for index, line in enumerate(lines):
        page.insert_text((72, 130 + index * 70), line, fontsize=28)
    document.save(str(path))
    document.close()


def _make_scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (1400, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (90, 130),
        "Markup Alpha Bravo",
        font=_font("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72),
        fill="black",
    )
    draw.text(
        (90, 300),
        "Scanned English fixture",
        font=_font("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56),
        fill="black",
    )
    image_path = path.with_suffix(".png")
    image.save(image_path, format="PNG")
    document = fitz.open()
    page = document.new_page(width=700, height=400)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(str(path))
    document.close()
    image_path.unlink()


def _make_multilingual_pdf(path: Path) -> dict[str, Any]:
    """Normalize an already-approved bilingual image into the product input shape."""

    image_path = APPROVED_ROOT / "images" / "1.jpeg"
    if not image_path.is_file():
        raise FileNotFoundError(f"approved bilingual fixture is missing: {image_path}")
    document = fitz.open()
    page = document.new_page(width=720, height=1018)
    page.insert_image(page.rect, filename=str(image_path))
    document.save(str(path))
    document.close()
    return {
        "kind": "approved-multilingual-image-normalized-to-pdf",
        "image_path_label": "approved-real-document/images/1.jpeg",
        "image_bytes": image_path.stat().st_size,
        "image_sha256": _sha256(image_path.read_bytes()),
    }


@contextmanager
def _selected_engine(name: str) -> Iterator[None]:
    previous = os.environ.get("OCR_MARKUP_ENGINE")
    os.environ["OCR_MARKUP_ENGINE"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OCR_MARKUP_ENGINE", None)
        else:
            os.environ["OCR_MARKUP_ENGINE"] = previous


def _failure_category(exc: Exception) -> str:
    mapping = {
        "TextNotFoundError": "TEXT_NOT_FOUND",
        "WordGeometryUnavailableError": "WORD_GEOMETRY_NOT_AVAILABLE",
        "AnnotationWriteError": "ANNOTATION_WRITE_FAILURE",
        "EngineUnavailableError": "ENGINE_UNAVAILABLE",
        "OCRTimeoutError": "TIMEOUT",
    }
    return mapping.get(type(exc).__name__, type(exc).__name__)


def _selection_summary(selection: Any) -> dict[str, Any]:
    rectangles = [
        [round(float(rect.x), 4), round(float(rect.y), 4), round(float(rect.x1), 4), round(float(rect.y1), 4)]
        for rect in selection.group_rects
    ]
    word_signature = "|".join(
        f"{word.id}\x1f{word.text}\x1f{word.bbox.x:.4f},{word.bbox.y:.4f},{word.bbox.x1:.4f},{word.bbox.y1:.4f}"
        for word in selection.words
    )
    return {
        "page_index": selection.page_index,
        "matched_text_sha256": _sha256(selection.matched_text),
        "word_ids": list(selection.word_ids),
        "reading_order_start": selection.reading_order_start,
        "reading_order_end": selection.reading_order_end,
        "word_count": len(selection.words),
        "word_geometry_sha256": _sha256(word_signature),
        "group_rects": rectangles,
        "source_type": _enum_value(selection.source_type),
        "provenance": list(selection.provenance),
    }


def _execution_summary(execution: Any) -> dict[str, Any]:
    return {
        "schema_version": "ocr_v2_markup_result.v1",
        "action": _enum_value(execution.action),
        "mode": _enum_value(execution.mode),
        "source_policy": execution.source_policy,
        "page_count": execution.page_count,
        "selection_count": len(execution.selections),
        "selections": [_selection_summary(selection) for selection in execution.selections],
        "page_sources": list(execution.page_sources),
    }


def _annotation_summary(source: Path, output: Path) -> dict[str, Any]:
    data = output.read_bytes()
    pages: list[dict[str, Any]] = []
    with fitz.open(str(source)) as source_document, fitz.open(str(output)) as output_document:
        if source_document.page_count != output_document.page_count:
            return {
                "header_valid": data[:5] == b"%PDF-",
                "byte_length": len(data),
                "sha256": _sha256(data),
                "page_count": output_document.page_count,
                "source_page_count": source_document.page_count,
            }
        for page_index, (source_page, output_page) in enumerate(zip(source_document, output_document)):
            annotations: list[dict[str, Any]] = []
            annotation_iterator = output_page.annots()
            if annotation_iterator:
                for annotation in annotation_iterator:
                    annotations.append(
                        {
                            "page_index": page_index,
                            "type": annotation.type[1],
                            "rect": [
                                round(float(annotation.rect.x0), 4),
                                round(float(annotation.rect.y0), 4),
                                round(float(annotation.rect.x1), 4),
                                round(float(annotation.rect.y1), 4),
                            ],
                        }
                    )
            source_raster = source_page.get_pixmap(alpha=False, annots=False)
            output_raster = output_page.get_pixmap(alpha=False, annots=False)
            pages.append(
                {
                    "page_index": page_index,
                    "width": round(float(output_page.rect.width), 4),
                    "height": round(float(output_page.rect.height), 4),
                    "source_width": round(float(source_page.rect.width), 4),
                    "source_height": round(float(source_page.rect.height), 4),
                    "source_image_count": len(source_page.get_images(full=True)),
                    "output_image_count": len(output_page.get_images(full=True)),
                    "source_text_sha256": _sha256(source_page.get_text("text")),
                    "output_text_sha256": _sha256(output_page.get_text("text")),
                    "source_raster_sha256": _sha256(source_raster.samples),
                    "output_raster_sha256": _sha256(output_raster.samples),
                    "annotations": annotations,
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


def _run_case(case: dict[str, Any], source: Path, engine_name: str, output: Path) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with _selected_engine(engine_name):
            execution = execute_ocr_markup(
                source,
                output,
                action=case["action"],
                query=case["query"],
                language=case["language"],
                language_mode=case["language_mode"],
                languages=case["languages"],
                mode="smart",
            )
        return {
            "engine": engine_name,
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "execution": _execution_summary(execution),
            "artifact": _annotation_summary(source, output),
        }
    except Exception as exc:
        return {
            "engine": engine_name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": {
                "category": _failure_category(exc),
                "type": type(exc).__name__,
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
        "schema_version": "pdfnest_ocr_markup_sdk_consumer_parity_environment.v1",
        "python": platform.python_version(),
        "revisions": revisions,
        "sdk_import": str(Path(platen_document.__file__).resolve()),
        "tesseract_binary": shutil.which("tesseract"),
        "tesseract": doctor.get("tesseract", {}),
        "capabilities": doctor.get("capabilities", {}),
        "execution": "sequential; direct markup family boundary; no PDFNest services; no managed resources",
        "engine_default": "internal",
    }


def _build_cases(temporary: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    native_single = temporary / "native-single.pdf"
    native_multiple = temporary / "native-multiple.pdf"
    scanned_single = temporary / "scanned-single.pdf"
    multilingual = temporary / "multilingual-approved.pdf"
    _make_native_pdf(native_single)
    _make_native_pdf(native_multiple, repeated=True)
    _make_scanned_pdf(scanned_single)
    multilingual_metadata = _make_multilingual_pdf(multilingual)

    base_cases = (
        ("highlight", "native_english", native_single, "Markup Alpha Bravo", "eng", ("eng",)),
        ("highlight", "scanned_english", scanned_single, "Markup Alpha", "eng", ("eng",)),
        ("highlight", "multilingual_explicit", multilingual, "Public Service Commission", "eng+sin", ("eng", "sin")),
        ("highlight", "multiple_occurrences", native_multiple, "Alpha", "eng", ("eng",)),
        ("highlight", "no_match", native_single, "Phrase Not Present", "eng", ("eng",)),
        ("underline", "native_english", native_single, "Markup Alpha Bravo", "eng", ("eng",)),
        ("underline", "scanned_english", scanned_single, "Markup Alpha", "eng", ("eng",)),
        ("underline", "multiple_occurrences", native_multiple, "Alpha", "eng", ("eng",)),
        ("underline", "no_match", native_single, "Phrase Not Present", "eng", ("eng",)),
        ("strikeout", "native_english", native_single, "Markup Alpha Bravo", "eng", ("eng",)),
        ("strikeout", "scanned_english", scanned_single, "Markup Alpha", "eng", ("eng",)),
        ("strikeout", "multiple_occurrences", native_multiple, "Alpha", "eng", ("eng",)),
        ("strikeout", "no_match", native_single, "Phrase Not Present", "eng", ("eng",)),
    )
    cases: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    for action, name, source, query, language, languages in base_cases:
        case = {
            "action": action,
            "name": f"{action}_{name}",
            "source": source,
            "query": query,
            "language": language,
            "language_mode": "EXPLICIT",
            "languages": languages,
        }
        cases.append(case)
        fixture = {
            "name": case["name"],
            "action": action,
            "source_sha256": _sha256(source.read_bytes()),
            "source_bytes": source.stat().st_size,
            "query_sha256": _sha256(query),
            "language": language,
            "language_mode": "EXPLICIT",
            "languages": list(languages),
            "source_kind": "approved-derived" if source == multilingual else "generated-regression-control",
        }
        if source == multilingual:
            fixture["approved_input"] = multilingual_metadata
        fixtures.append(fixture)
    return cases, fixtures


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pdfnest-ocr-markup-parity-") as temporary_dir:
        temporary = Path(temporary_dir)
        cases, fixtures = _build_cases(temporary)
        for case in cases:
            internal = _run_case(case, case["source"], "internal", temporary / f"{case['name']}-internal.pdf")
            sdk = _run_case(case, case["source"], "sdk", temporary / f"{case['name']}-sdk.pdf")
            if internal["ok"] and sdk["ok"]:
                differing_fields = _differing_paths(
                    {
                        "execution": internal["execution"],
                        "artifact": _artifact_semantics(internal["artifact"]),
                    },
                    {
                        "execution": sdk["execution"],
                        "artifact": _artifact_semantics(sdk["artifact"]),
                    },
                )
                matched = not differing_fields
                comparison = {
                    "matched_fields": {
                        "execution": not any(path.startswith("execution") for path in differing_fields),
                        "artifact_semantics": not any(path.startswith("artifact") for path in differing_fields),
                    },
                    "differing_fields": differing_fields,
                }
            elif not internal["ok"] and not sdk["ok"]:
                differing_fields = _differing_paths(internal["error"], sdk["error"], "error")
                matched = not differing_fields
                comparison = {
                    "matched_fields": {"failure_contract": matched},
                    "differing_fields": differing_fields,
                }
            else:
                matched = False
                comparison = {
                    "matched_fields": {"execution_outcome": False},
                    "differing_fields": ["execution.ok"],
                }
            records.append(
                {
                    "case": case["name"],
                    "action": case["action"],
                    "fixture_sha256": _sha256(case["source"].read_bytes()),
                    "internal": internal,
                    "sdk": sdk,
                    "parity": "MATCH" if matched else "MISMATCH",
                    **comparison,
                    "artifact_byte_difference": (
                        {
                            "byte_length_equal": internal["artifact"]["byte_length"] == sdk["artifact"]["byte_length"],
                            "sha256_equal": internal["artifact"]["sha256"] == sdk["artifact"]["sha256"],
                        }
                        if internal["ok"] and sdk["ok"]
                        else None
                    ),
                }
            )

    summary = {
        "schema_version": "pdfnest_ocr_markup_sdk_consumer_parity.v1",
        "classification": "CONSUMER_BOUNDARY_PARITY",
        "execution": "sequential; Highlight/Underline/Strikeout family selector; no PDFNest services; no managed resources",
        "engines": ["internal", "sdk"],
        "case_count": len(records),
        "matched_cases": sum(record["parity"] == "MATCH" for record in records),
        "mismatched_cases": sum(record["parity"] != "MATCH" for record in records),
        "material_mismatches": 0 if all(record["parity"] == "MATCH" for record in records) else "REVIEW_REQUIRED",
        "records": records,
    }
    (OUTPUT_ROOT / "fixture-inventory.json").write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "parity-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT_ROOT / "run-metadata.json").write_text(json.dumps(_environment_metadata(), indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: summary[key] for key in ("case_count", "matched_cases", "mismatched_cases")}, sort_keys=True))
    return 0 if summary["mismatched_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

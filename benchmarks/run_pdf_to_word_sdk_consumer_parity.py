#!/usr/bin/env python3
"""Run a bounded PDF-to-Word internal-vs-SDK consumer parity comparison.

This harness deliberately compares the product's semantic DOCX projection and
canonical structured-result summaries.  It records raw artifact identity only
as evidence; ZIP bytes are not treated as the behavioral contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import fitz

from app.api.tools.pdf_to_office.converters.word import (
    _requires_structured_ocr,
    _write_structured_result_to_word,
    convert_to_word,
)
from app.core.pdf_to_word_ocr_engine import execute_pdf_to_word_ocr


WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _source_metadata(path: Path, *, derived_from: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        with fitz.open(str(path)) as document:
            page_count = len(document)
            dimensions = [
                {"width": round(page.rect.width, 3), "height": round(page.rect.height, 3)}
                for page in document
            ]
    except Exception as exc:
        page_count = None
        dimensions = []
        open_error_type = type(exc).__name__
    else:
        open_error_type = None
    value: dict[str, Any] = {
        "name": path.name,
        "sha256": _sha256(path),
        "page_count": page_count,
        "dimensions": dimensions,
    }
    if open_error_type:
        value["open_error_type"] = open_error_type
    if derived_from:
        value["derived_from"] = derived_from
    return value


def _make_page_subset(source: Path, destination: Path, page_index: int = 0) -> Path:
    with fitz.open(str(source)) as source_document:
        with fitz.open() as subset:
            subset.insert_pdf(source_document, from_page=page_index, to_page=page_index)
            subset.save(str(destination))
    return destination


def _structured_summary(result: Any) -> dict[str, Any]:
    page_summaries: list[dict[str, Any]] = []
    all_type_counts: Counter[str] = Counter()
    text_hash_inputs: list[str] = []
    for page in result.pages:
        page_types: Counter[str] = Counter()
        element_hashes: list[str] = []
        for element_id in page.reading_order:
            element = next(item for item in page.elements if item.element_id == element_id)
            element_type = str(getattr(getattr(element, "type", None), "value", getattr(element, "type", "")))
            page_types[element_type] += 1
            all_type_counts[element_type] += 1
            text = getattr(element, "text", "") or ""
            text_hash_inputs.append(text)
            element_hashes.append(_json_hash({"id": element_id, "type": element_type, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}))
        language = getattr(page, "language", {}) or {}
        page_summaries.append(
            {
                "page_index": page.page_index,
                "classification": page.classification,
                "processing_source": page.processing_source,
                "status": page.status,
                "element_counts": dict(sorted(page_types.items())),
                "reading_order_count": len(page.reading_order),
                "element_identity_hash": _json_hash(element_hashes),
                "language": {
                    "requested": list(language.get("requested", [])),
                    "detected": list(language.get("detected", [])),
                    "status": language.get("status"),
                    "mode": language.get("mode"),
                },
                "warnings": list(getattr(page, "warnings", ()) or ()),
            }
        )
    validation = getattr(result, "validation", {}) or {}
    return {
        "schema_version": getattr(result, "schema_version", None),
        "page_count": len(result.pages),
        "page_summaries": page_summaries,
        "element_counts": dict(sorted(all_type_counts.items())),
        "reading_order_hash": _json_hash([list(page.reading_order) for page in result.pages]),
        "text_sha256": hashlib.sha256("\n".join(text_hash_inputs).encode()).hexdigest(),
        "text_chars": sum(len(text) for text in text_hash_inputs),
        "warnings": list(getattr(result, "warnings", ()) or ()),
        "validation_valid": validation.get("valid"),
    }


def _docx_summary(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        document_root = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs: list[str] = []
        for paragraph in document_root.findall(".//w:body/w:p", WORD_NS):
            paragraphs.append("".join(node.text or "" for node in paragraph.findall(".//w:t", WORD_NS)))
        tables: list[list[list[str]]] = []
        for table in document_root.findall(".//w:tbl", WORD_NS):
            rows: list[list[str]] = []
            for row in table.findall("./w:tr", WORD_NS):
                rows.append(
                    [
                        "".join(node.text or "" for node in cell.findall(".//w:t", WORD_NS))
                        for cell in row.findall("./w:tc", WORD_NS)
                    ]
                )
            tables.append(rows)
        relationships = archive.read("word/_rels/document.xml.rels")
        image_count = sum(name.startswith("word/media/") for name in names)
    text = "\n".join(paragraphs)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "valid_zip": True,
        "required_entries": all(name in names for name in ("[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels")),
        "paragraph_count": len(paragraphs),
        "table_count": len(tables),
        "table_shapes": [[len(row) for row in table] for table in tables],
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text_chars": len(text),
        "non_ascii_chars": sum(ord(char) > 127 for char in text),
        "page_breaks": len(document_root.findall('.//w:br[@w:type="page"]', WORD_NS)),
        "image_count": image_count,
        "relationships_sha256": hashlib.sha256(relationships).hexdigest(),
    }


def _semantic_docx_fields(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in (
        "valid_zip",
        "required_entries",
        "paragraph_count",
        "table_count",
        "table_shapes",
        "text_sha256",
        "text_chars",
        "non_ascii_chars",
        "page_breaks",
        "image_count",
    )}


def _safe_error(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "classification": "controlled_conversion_failure"}


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


def _run_case(case: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    source = Path(case["source"])
    route_error: dict[str, str] | None = None
    try:
        route = _requires_structured_ocr(str(source))
    except Exception as exc:
        # A malformed-input control has no route; each selected engine is
        # still exercised through the product converter below.
        route = False
        route_error = _safe_error(exc)
    case_result: dict[str, Any] = {
        "name": case["name"],
        "language": case["language"],
        "requires_structured_ocr": None if route_error else route,
        "source": _source_metadata(source),
        "engines": {},
    }
    if route_error:
        case_result["route_error"] = route_error
    for selected_engine in ("internal", "sdk"):
        os.environ["PDF_TO_WORD_OCR_ENGINE"] = selected_engine
        artifact_path = output_dir / "artifacts" / f"{case['name']}-{selected_engine}.docx"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        engine_result: dict[str, Any] = {"engine": selected_engine}
        try:
            if route:
                canonical = execute_pdf_to_word_ocr(str(source), language=case["language"])
                engine_result["canonical"] = _structured_summary(canonical)
                _write_structured_result_to_word(canonical, str(artifact_path))
            else:
                convert_to_word(str(source), str(artifact_path), language=case["language"])
                engine_result["canonical"] = {"available": False, "route": "native_pdf2docx"}
            engine_result["docx"] = _docx_summary(artifact_path)
        except Exception as exc:
            engine_result["error"] = _safe_error(exc)
        case_result["engines"][selected_engine] = engine_result

    internal = case_result["engines"]["internal"]
    sdk = case_result["engines"]["sdk"]
    if "error" in internal or "error" in sdk:
        case_result["semantic_match"] = internal.get("error") == sdk.get("error")
        case_result["canonical_match"] = case_result["semantic_match"]
        case_result["docx_semantic_match"] = None
        case_result["difference"] = "both controlled failure" if case_result["semantic_match"] else "different failure behavior"
    else:
        internal_docx = internal["docx"]
        sdk_docx = sdk["docx"]
        internal_canonical = internal.get("canonical")
        sdk_canonical = sdk.get("canonical")
        case_result["canonical_match"] = internal_canonical == sdk_canonical
        case_result["differing_canonical_fields"] = _differing_paths(internal_canonical, sdk_canonical)
        case_result["docx_semantic_match"] = _semantic_docx_fields(internal_docx) == _semantic_docx_fields(sdk_docx)
        case_result["semantic_match"] = case_result["canonical_match"] and case_result["docx_semantic_match"]
        case_result["difference"] = (
            "raw DOCX container identity differs; semantic fields match"
            if internal_docx["sha256"] != sdk_docx["sha256"] and case_result["semantic_match"]
            else "canonical result differs"
            if not case_result["canonical_match"]
            else "DOCX semantic fields differ"
            if not case_result["docx_semantic_match"]
            else None
        )
        case_result["matched_semantic_fields"] = [
            key for key in _semantic_docx_fields(internal_docx)
            if _semantic_docx_fields(internal_docx)[key] == _semantic_docx_fields(sdk_docx)[key]
        ]
        case_result["differing_semantic_fields"] = [
            key for key in _semantic_docx_fields(internal_docx)
            if _semantic_docx_fields(internal_docx)[key] != _semantic_docx_fields(sdk_docx)[key]
        ]
    return case_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/home/gimesha/My_Projects/platen/output/document-sdk-pdf-to-word-consumer-01"),
    )
    args = parser.parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf-to-word-parity-", dir="/tmp") as temp_dir:
        temp = Path(temp_dir)
        approved = Path("/home/gimesha/pdfnest-tests")
        derived: dict[str, dict[str, Any]] = {}
        for name, source_name in (
            ("sinhala_page", "SINHALA HANDBOOK.pdf"),
            ("bilingual_page", "2026.08.14 Part I-II A (S) TEM._1786695134.pdf"),
            ("native_table_page", "file_example_XLS_1000 (1).pdf"),
        ):
            source = approved / source_name
            subset = _make_page_subset(source, temp / f"{name}.pdf")
            derived[name] = {
                "path": str(subset),
                "metadata": _source_metadata(subset, derived_from=_source_metadata(source)),
            }
        cases = [
            {"name": "native_english", "source": str(approved / "chatgpt.com_layout.pdf"), "language": "eng"},
            {"name": "scanned_english", "source": str(approved / "compiled-images.pdf"), "language": "eng"},
            {"name": "explicit_sinhala", "source": derived["sinhala_page"]["path"], "language": "sin"},
            {"name": "explicit_eng_sin", "source": derived["bilingual_page"]["path"], "language": "eng+sin"},
            {"name": "auto_bilingual", "source": derived["bilingual_page"]["path"], "language": "auto"},
            {"name": "mixed_native_scanned", "source": str(approved / "Admission-studio (4).pdf"), "language": "eng"},
            {"name": "scanned_structure", "source": str(approved / "ocr-extracted-text-29-rotated (1).pdf"), "language": "eng"},
            {"name": "native_table", "source": derived["native_table_page"]["path"], "language": "eng"},
        ]
        malformed = temp / "malformed.pdf"
        malformed.write_bytes(b"not a PDF")
        cases.append({"name": "malformed_input", "source": str(malformed), "language": "eng"})
        inventory = []
        for case in cases:
            source = Path(case["source"])
            inventory.append({"name": case["name"], "language": case["language"], "source": _source_metadata(source)})
        _write_json(output_dir / "fixture-inventory.json", inventory)
        _write_json(output_dir / "call-chain.json", {
            "native": "Go POST /api/conversion/pdf-to-word -> worker /api/v1/office/convert -> convert_to_word -> isolated pdf2docx",
            "ocr_fallback": "convert_to_word -> _requires_structured_ocr -> PDF_TO_WORD_OCR_ENGINE -> internal StructuredDocumentProcessor or sdk platen_document.DocumentProcessor.extract_document -> PDFNest python-docx projection",
            "application_owner": ["auth", "billing", "upload", "temporary files", "HTTP response", "cleanup"],
        })
        results: list[dict[str, Any]] = []
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['name']}", flush=True)
            results.append(_run_case(case, output_dir))
        _write_json(output_dir / "parity-summary.json", {
            "cases": results,
            "case_count": len(results),
            "passed_cases": sum(bool(item.get("semantic_match")) for item in results),
            "material_mismatches": [item["name"] for item in results if not item.get("semantic_match")],
            "raw_container_differences": [
                item["name"]
                for item in results
                if item.get("difference") == "raw DOCX container identity differs; semantic fields match"
            ],
            "canonical_mismatches": [item["name"] for item in results if not item.get("canonical_match")],
            "docx_semantic_mismatches": [item["name"] for item in results if item.get("docx_semantic_match") is False],
        })
        _write_json(output_dir / "initial-parity-analysis.json", results)
        _write_json(output_dir / "docx-validation.json", {
            "cases": [{"name": item["name"], "semantic_match": item.get("semantic_match"), "difference": item.get("difference")} for item in results],
            "validation_contract": ["valid_zip", "required_entries", "paragraph_count", "table_count", "table_shapes", "text_sha256", "text_chars", "non_ascii_chars", "page_breaks", "image_count"],
        })
        _write_json(output_dir / "native-result.json", next(item for item in results if item["name"] == "native_english"))
        _write_json(output_dir / "scanned-result.json", next(item for item in results if item["name"] == "scanned_english"))
        _write_json(output_dir / "mixed-result.json", next(item for item in results if item["name"] == "mixed_native_scanned"))
        _write_json(output_dir / "table-result.json", next(item for item in results if item["name"] == "native_table"))
        _write_json(output_dir / "multilingual-result.json", [item for item in results if item["name"] in {"explicit_sinhala", "explicit_eng_sin", "auto_bilingual"}])
    print(f"parity_summary={output_dir / 'parity-summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

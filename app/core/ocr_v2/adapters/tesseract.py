"""Explicit-language Tesseract OCR V2 adapter."""

from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz
from PIL import Image

from ..contracts import UnnormalizedPageOutput
from ..errors import ConfigurationError, EngineUnavailableError, OCRTimeoutError
from ..geometry import PreparedRaster
from ..language_policy import (
    LanguagePolicyError,
    LanguageProbe,
    OCRLanguagePolicy,
    script_counts,
    scripts_for_languages,
)
from ..table_ocr import prepare_table_ocr_image
from ...subprocess_runner import run_hardened_subprocess
from ...tesseract_capacity import acquire_tesseract_capacity
from .base import EngineAdapter, EngineAvailability, provenance_from_description


def _tsv_words(tsv: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        text = str(row.get("text", "")).strip()
        try:
            confidence = float(row.get("conf", "-1"))
            left, top, width, height = (float(row.get(name, "0")) for name in ("left", "top", "width", "height"))
        except (TypeError, ValueError):
            continue
        if not text or width <= 0 or height <= 0 or confidence < 0:
            continue
        line = tuple(row.get(name, "0") for name in ("block_num", "par_num", "line_num"))
        grouped[line].append({"text": text, "bbox": [left, top, left + width, top + height], "confidence": confidence})
    return grouped


def _recover_minority_words(
    primary: dict[tuple[str, str, str], list[dict[str, Any]]],
    combined: dict[tuple[str, str, str], list[dict[str, Any]]],
    minority_scripts: set[str],
    cancellation_check: Callable[[], None] | None,
) -> None:
    # The pair owns the layout and reading order. A third-script word may
    # replace a contiguous, weaker span on one line if its box aligns.
    # Reject insertions without an anchor, cross-line matches and partial
    # overlaps with neighboring text.
    bounds = {
        key: fitz.Rect(
            min(word["bbox"][0] for word in words), min(word["bbox"][1] for word in words),
            max(word["bbox"][2] for word in words), max(word["bbox"][3] for word in words),
        )
        for key, words in primary.items()
    }
    single_scripts = [{script} for script in minority_scripts]
    for words in combined.values():
        if cancellation_check:
            cancellation_check()
        for word in words:
            letters = "".join(char for char in word["text"] if unicodedata.category(char)[0] in {"L", "M"})
            if (
                word["confidence"] < 70
                or len(letters) < 2
                or not any(char.isalpha() for char in letters)
                or set(script_counts(letters)) not in single_scripts
            ):
                continue
            box = fitz.Rect(word["bbox"])
            conflicts = []
            for key, line in bounds.items():
                if not line.intersects(box):
                    continue
                for index, old_word in enumerate(primary[key]):
                    old_box = fitz.Rect(old_word["bbox"])
                    area = (box & old_box).get_area()
                    if area > 0:
                        conflicts.append((key, index, area, old_box, old_word))
            if not conflicts or len({key for key, *_ in conflicts}) != 1:
                continue
            key, first, *_ = conflicts[0]
            last = conflicts[-1][1]
            if last - first + 1 != len(conflicts) or any(
                area < 0.5 * min(box.get_area(), old_box.get_area())
                or word["confidence"] <= old_word["confidence"]
                or old_word.get("recovered")
                for _, _, area, old_box, old_word in conflicts
            ):
                continue
            primary[key][first:last + 1] = [{**word, "recovered": True}]
            bounds[key] |= box


class TesseractAdapter(EngineAdapter):
    """Use the installed Tesseract binary and TSV output."""

    def __init__(
        self,
        languages: str,
        *,
        timeout: float = 300.0,
        tessdata_dir: str | None = None,
        table_aware_ocr: bool = False,
    ) -> None:
        if not languages or not languages.strip() or languages.strip().lower() in {"auto", "automatic", "detect"}:
            raise ConfigurationError("TesseractAdapter requires a resolved explicit language policy")
        try:
            self.policy = OCRLanguagePolicy.from_request(languages)
        except LanguagePolicyError as exc:
            raise ConfigurationError(str(exc)) from exc
        self.languages = self.policy.engine_expression
        self.timeout = timeout
        self.tessdata_dir = Path(tessdata_dir or os.getenv("TESSDATA_PREFIX", "")) if (tessdata_dir or os.getenv("TESSDATA_PREFIX")) else None
        self.table_aware_ocr = table_aware_ocr
        self._ready = False

    def describe(self) -> dict[str, Any]:
        return {
            "id": "tesseract_v2",
            "version": "system-tesseract",
            "source": "tesseract-tsv",
            "configuration": {"languages": list(self.policy.languages), "language_expression": self.languages, "timeout_seconds": self.timeout, "table_aware_ocr": self.table_aware_ocr},
            "capabilities": ["TEXT", "WORD_GEOMETRY", "LINE_GEOMETRY", "BLOCK_GEOMETRY", "CONFIDENCE", "LANGUAGE_METADATA", "READING_ORDER", "PAGE_INDEPENDENT"],
        }

    def _data_dir(self) -> Path | None:
        candidates = [self.tessdata_dir] if self.tessdata_dir else []
        candidates.extend(Path(path) for path in ("/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata", "/usr/share/tessdata", "/usr/local/share/tessdata"))
        for candidate in candidates:
            if candidate and candidate.is_dir():
                return candidate
        return None

    def availability(self) -> EngineAvailability:
        if shutil.which("tesseract") is None:
            return EngineAvailability(False, "tesseract binary is not installed")
        data_dir = self._data_dir()
        if data_dir is None:
            return EngineAvailability(False, "tesseract tessdata directory is not available")
        missing = [part for part in self.languages.split("+") if not (data_dir / f"{part}.traineddata").is_file()]
        if missing:
            return EngineAvailability(False, f"missing explicit Tesseract language model(s): {', '.join(missing)}")
        return EngineAvailability(True)

    def initialize(self) -> None:
        detail = self.availability()
        if not detail.available:
            raise EngineUnavailableError(detail.reason)
        self._ready = True

    def readiness(self) -> bool:
        return self._ready and self.availability().available

    def probe(self, image: Image.Image, languages: tuple[str, ...]) -> LanguageProbe:
        """Run one bounded text-only probe used by the shared AUTO detector."""
        expression = "+".join(languages)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image.save(image_file, format="PNG")
            image_path = image_file.name
        try:
            env = os.environ.copy()
            data_dir = self._data_dir()
            if data_dir:
                env["TESSDATA_PREFIX"] = str(data_dir)
            command = ["tesseract", image_path, "stdout", "-l", expression, "--psm", "6", "tsv"]
            with acquire_tesseract_capacity(timeout=self.timeout):
                completed = run_hardened_subprocess(command, timeout=self.timeout, env=env)
            if completed.returncode != 0:
                return LanguageProbe(languages, "", -1.0)
            rows = list(csv.DictReader(io.StringIO(completed.stdout), delimiter="\t"))
            words = [str(row.get("text", "")).strip() for row in rows if str(row.get("text", "")).strip()]
            confidences = []
            confident_script_words: Counter[str] = Counter()
            for row in rows:
                try:
                    confidence = float(row.get("conf", "-1"))
                except (TypeError, ValueError):
                    continue
                if confidence >= 0:
                    confidences.append(confidence)
                if confidence >= 70:
                    # Ignore punctuation and digits, and exclude mixed-script
                    # words. A stray character is insufficient evidence.
                    letters_and_marks = "".join(
                        char for char in (row.get("text") or "")
                        if unicodedata.category(char)[0] in {"L", "M"}
                    )
                    scripts = script_counts(letters_and_marks)
                    if (
                        len(letters_and_marks) >= 2
                        and any(char.isalpha() for char in letters_and_marks)
                        and len(scripts) == 1
                        and "OTHER" not in scripts
                    ):
                        confident_script_words.update(scripts.keys())
            average = sum(confidences) / len(confidences) if confidences else -1.0
            return LanguageProbe(
                languages, " ".join(words), average,
                confident_script_words=dict(confident_script_words),
            )
        finally:
            try:
                Path(image_path).unlink()
            except OSError:
                pass

    def recognize_page(self, page_id: str, raster: PreparedRaster) -> UnnormalizedPageOutput:
        return self._recognize_page(page_id, raster)

    def recognize_page_with_minority(
        self,
        page_id: str,
        raster: PreparedRaster,
        primary_languages: tuple[str, ...],
        *,
        cancellation_check: Callable[[], None] | None = None,
        deadline: float | None = None,
    ) -> UnnormalizedPageOutput:
        local_deadline = time.monotonic() + self.timeout
        return self._recognize_page(
            page_id, raster, primary_languages, cancellation_check,
            min(deadline, local_deadline) if deadline is not None else local_deadline,
        )

    def _recognize_page(
        self,
        page_id: str,
        raster: PreparedRaster,
        primary_languages: tuple[str, ...] = (),
        cancellation_check: Callable[[], None] | None = None,
        deadline: float | None = None,
    ) -> UnnormalizedPageOutput:
        if not self.readiness():
            self.initialize()
        ocr_image = raster.image
        table_grid = None
        # The conservative line detector is defined for the existing upright
        # OCR image contract.  Preserve ordinary rotated-page OCR until an
        # orientation-aware table path is designed and tested.
        if self.table_aware_ocr and raster.geometry.rotation == 0:
            ocr_image, table_grid = prepare_table_ocr_image(raster.image)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            if table_grid is None:
                image_file.write(raster.png_bytes)
            else:
                ocr_image.save(image_file, format="PNG", dpi=(raster.dpi, raster.dpi))
            image_path = image_file.name
        try:
            env = os.environ.copy()
            # Availability resolves an explicit override through the known
            # system fallbacks. Pass that same resolved directory to the
            # subprocess; otherwise a stale/invalid override can make the
            # availability check succeed while Tesseract still cannot load
            # the requested language data.
            data_dir = self._data_dir()
            if data_dir:
                env["TESSDATA_PREFIX"] = str(data_dir)
            def remaining() -> float:
                if deadline is None:
                    return self.timeout
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise OCRTimeoutError("OCR exceeded the page recognition deadline")
                return seconds

            def recognize(languages: str) -> str:
                if cancellation_check:
                    cancellation_check()
                command = ["tesseract", image_path, "stdout", "-l", languages]
                if table_grid is not None:
                    command.extend(["--psm", "6"])
                command.append("tsv")
                with acquire_tesseract_capacity(timeout=remaining()):
                    completed = run_hardened_subprocess(command, timeout=remaining(), env=env)
                if completed.returncode != 0:
                    raise EngineUnavailableError(completed.stderr.strip() or "Tesseract returned a non-zero status")
                return completed.stdout

            primary_tsv = recognize("+".join(primary_languages) if primary_languages else self.languages)
            grouped = _tsv_words(primary_tsv)
            raw = {"tsv": primary_tsv}
            if primary_languages:
                combined_tsv = recognize(self.languages)
                minority = scripts_for_languages(self.policy.languages) - scripts_for_languages(primary_languages)
                _recover_minority_words(grouped, _tsv_words(combined_tsv), minority, cancellation_check)
                raw["supplemental_tsv"] = combined_tsv
            items: list[dict[str, Any]] = []
            for key, words in grouped.items():
                for word in words:
                    word.pop("recovered", None)
                    word.update(
                        id=f"{page_id}-token-{len(items)}", confidence_scale="0_100",
                        block_id=key[0], line_id=":".join(key),
                    )
                    items.append(word)
            lines = []
            for key, words in grouped.items():
                lines.append({
                    "id": f"{page_id}-line-{'-'.join(key)}",
                    "text": " ".join(item["text"] for item in words),
                    "bbox": [min(item["bbox"][0] for item in words), min(item["bbox"][1] for item in words), max(item["bbox"][2] for item in words), max(item["bbox"][3] for item in words)],
                    "token_ids": [item["id"] for item in words],
                })
            items.extend({"kind": "line", **line} for line in lines)
            text = "\n".join(line["text"] for line in lines)
            description = self.describe()
            return UnnormalizedPageOutput(
                page_id=page_id,
                text=text,
                items=tuple(items),
                coordinate_space="pixel_top_left",
                provenance=provenance_from_description(description),
                raw_output=raw,
                metadata={"pixel_width": raster.image.width, "pixel_height": raster.image.height, "requested_languages": self.languages},
            )
        finally:
            try:
                Path(image_path).unlink()
            except OSError:
                pass

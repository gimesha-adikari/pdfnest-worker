"""Explicit-language Tesseract OCR V2 adapter."""

from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..contracts import UnnormalizedPageOutput
from ..errors import ConfigurationError, EngineUnavailableError
from ..geometry import PreparedRaster
from ...subprocess_runner import run_hardened_subprocess
from ...tesseract_capacity import acquire_tesseract_capacity
from .base import EngineAdapter, EngineAvailability, provenance_from_description


class TesseractAdapter(EngineAdapter):
    """Use the installed Tesseract binary and TSV output without auto-detect."""

    def __init__(self, languages: str, *, timeout: float = 300.0, tessdata_dir: str | None = None) -> None:
        if not languages or not languages.strip() or languages.strip().lower() in {"auto", "detect"}:
            raise ConfigurationError("OCR V2 requires explicit Tesseract language(s); auto-detection is not allowed")
        self.languages = "+".join(part.strip() for part in languages.split("+") if part.strip())
        self.timeout = timeout
        self.tessdata_dir = Path(tessdata_dir or os.getenv("TESSDATA_PREFIX", "")) if (tessdata_dir or os.getenv("TESSDATA_PREFIX")) else None
        self._ready = False

    def describe(self) -> dict[str, Any]:
        return {
            "id": "tesseract_v2",
            "version": "system-tesseract",
            "source": "tesseract-tsv",
            "configuration": {"languages": self.languages, "timeout_seconds": self.timeout},
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

    def recognize_page(self, page_id: str, raster: PreparedRaster) -> UnnormalizedPageOutput:
        if not self.readiness():
            self.initialize()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_file.write(raster.png_bytes)
            image_path = image_file.name
        try:
            env = os.environ.copy()
            if self.tessdata_dir:
                env["TESSDATA_PREFIX"] = str(self.tessdata_dir)
            command = ["tesseract", image_path, "stdout", "-l", self.languages, "tsv"]
            with acquire_tesseract_capacity(timeout=self.timeout):
                completed = run_hardened_subprocess(command, timeout=self.timeout, env=env)
            if completed.returncode != 0:
                raise EngineUnavailableError(completed.stderr.strip() or "Tesseract returned a non-zero status")
            rows = list(csv.DictReader(io.StringIO(completed.stdout), delimiter="\t"))
            grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
            items: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                text = str(row.get("text", "")).strip()
                try:
                    confidence = float(row.get("conf", "-1"))
                    box = [float(row.get(name, "0")) for name in ("left", "top", "left", "top")]
                    box[2] = box[0] + float(row.get("width", "0"))
                    box[3] = box[1] + float(row.get("height", "0"))
                except (TypeError, ValueError):
                    continue
                if not text or box[2] <= box[0] or box[3] <= box[1] or confidence < 0:
                    continue
                item = {
                    "id": f"{page_id}-token-{len(items)}",
                    "text": text,
                    "bbox": box,
                    "confidence": confidence,
                    "confidence_scale": "0_100",
                    "block_id": str(row.get("block_num", "0")),
                    "line_id": f"{row.get('block_num', '0')}:{row.get('par_num', '0')}:{row.get('line_num', '0')}",
                }
                items.append(item)
                grouped[(item["block_id"], str(row.get("par_num", "0")), str(row.get("line_num", "0")))].append(item)
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
                raw_output={"tsv": completed.stdout},
                metadata={"pixel_width": raster.image.width, "pixel_height": raster.image.height, "requested_languages": self.languages},
            )
        finally:
            try:
                Path(image_path).unlink()
            except OSError:
                pass

"""Optional CPU PP-OCRv6 Medium adapter.

Paddle packages are intentionally not worker dependencies. Imports and model
availability checks remain lazy so the default worker image is unchanged.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

from ..contracts import UnnormalizedPageOutput
from ..errors import EngineUnavailableError
from ..geometry import PreparedRaster
from .base import EngineAdapter, EngineAvailability, provenance_from_description


DET_NAME = "PP-OCRv6_medium_det"
REC_NAME = "PP-OCRv6_medium_rec"


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


class PPOCRv6MediumAdapter(EngineAdapter):
    """Frozen Milestone 2I CPU configuration, with no implicit downloads."""

    def __init__(self, model_root: str | Path | None = None) -> None:
        configured = model_root or os.getenv("PPOCRV6_MODEL_ROOT")
        self.model_root = Path(configured).expanduser().resolve() if configured else Path.home() / ".paddlex" / "official_models"
        self.det_dir = self.model_root / DET_NAME
        self.rec_dir = self.model_root / REC_NAME
        self._ocr: Any = None

    def describe(self) -> dict[str, Any]:
        return {
            "id": "ppocrv6_medium_v2",
            "version": _version("paddleocr") or "PaddleOCR 3.7.0 (required)",
            "source": "PaddleOCR CPU paddle_static",
            "model_identity": f"{DET_NAME}+{REC_NAME}",
            "configuration": {
                "paddlepaddle": "3.2.0",
                "paddleocr": "3.7.0",
                "paddlex": "3.7.2",
                "engine": "paddle_static",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "return_word_box": True,
                "lang": None,
                "device": "cpu",
            },
        }

    def availability(self) -> EngineAvailability:
        missing = [name for name in ("paddlepaddle", "paddleocr", "paddlex") if _version(name) is None]
        missing.extend(path.name for path in (self.det_dir, self.rec_dir) if not path.is_dir())
        if missing:
            return EngineAvailability(False, "PP-OCRv6 Medium unavailable: " + ", ".join(missing))
        return EngineAvailability(True)

    def initialize(self) -> None:
        detail = self.availability()
        if not detail.available:
            raise EngineUnavailableError(detail.reason)
        from paddleocr import PaddleOCR  # type: ignore[import-not-found]

        self._ocr = PaddleOCR(
            text_detection_model_name=DET_NAME,
            text_detection_model_dir=str(self.det_dir),
            text_recognition_model_name=REC_NAME,
            text_recognition_model_dir=str(self.rec_dir),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            return_word_box=True,
            lang=None,
        )

    def readiness(self) -> bool:
        return self._ocr is not None

    @staticmethod
    def _pixel_box(box: list[float], raster: PreparedRaster) -> list[float]:
        x0, y0, x1, y1 = (float(value) for value in box)
        return [x0, y0, x1, y1]

    @staticmethod
    def _union(left: list[float] | None, right: list[float]) -> list[float]:
        if left is None:
            return list(right)
        return [min(left[0], right[0]), min(left[1], right[1]), max(left[2], right[2]), max(left[3], right[3])]

    def recognize_page(self, page_id: str, raster: PreparedRaster) -> UnnormalizedPageOutput:
        if not self.readiness():
            self.initialize()
        result = self._ocr.predict(raster.image)[0]
        payload = getattr(result, "json", None)
        raw = payload.get("res", payload) if isinstance(payload, dict) else payload
        raw = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
        texts = [str(value) for value in raw.get("rec_texts", [])]
        boxes = raw.get("rec_boxes", [])
        scores = [float(value) for value in raw.get("rec_scores", [])]
        items: list[dict[str, Any]] = []
        word_fragments = raw.get("text_word", [])
        word_boxes = raw.get("text_word_boxes", [])
        if word_fragments and word_boxes:
            for line_fragments, line_boxes, score in zip(word_fragments, word_boxes, scores or [0.0] * len(word_fragments)):
                value = ""
                union: list[float] | None = None
                for fragment, box in zip(line_fragments, line_boxes):
                    fragment_text = str(fragment)
                    if fragment_text.strip() and len(box) == 4:
                        value += fragment_text.strip()
                        union = self._union(union, self._pixel_box(box, raster))
                    if fragment_text.endswith((" ", "\n", "\r", "\t")) and value and union is not None:
                        items.append({"id": f"{page_id}-token-{len(items)}", "text": value, "bbox": union, "confidence": score, "confidence_scale": "0_1"})
                        value, union = "", None
                if value and union is not None:
                    items.append({"id": f"{page_id}-token-{len(items)}", "text": value, "bbox": union, "confidence": score, "confidence_scale": "0_1"})
        else:
            # rec_boxes are line/detection boxes, not word geometry. Preserve
            # them as lines so the normalizer cannot advertise fake words.
            for index, (text, box) in enumerate(zip(texts, boxes)):
                if not text.strip() or len(box) != 4:
                    continue
                items.append({"kind": "line", "id": f"{page_id}-line-{index}", "text": text, "bbox": self._pixel_box(box, raster), "token_ids": []})
        description = self.describe()
        return UnnormalizedPageOutput(
            page_id=page_id,
            text=" ".join(texts),
            items=tuple(items),
            coordinate_space="pixel_top_left",
            provenance=provenance_from_description(description),
            raw_output=raw,
            metadata={"pixel_width": raster.image.width, "pixel_height": raster.image.height},
        )

    def shutdown(self) -> None:
        self._ocr = None

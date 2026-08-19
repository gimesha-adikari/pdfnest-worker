from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import fitz

from app.core.subprocess_runner import run_hardened_subprocess

logger = logging.getLogger(__name__)

TESSDATA_CANDIDATES = [
    os.getenv("TESSDATA_PREFIX", ""),
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
]


def get_tessdata_dir() -> str:
    for d in TESSDATA_CANDIDATES:
        if d and os.path.isdir(d):
            return d
    return "/usr/share/tesseract-ocr/5/tessdata"


def validate_ocr_language(lang: str) -> str:
    """
    Validates that requested Tesseract language models exist on worker.
    Raises ValueError if requested traineddata file is missing.
    """
    if not lang:
        return "eng"

    tessdata_dir = get_tessdata_dir()
    valid_langs = []
    missing_langs = []

    for l in lang.split("+"):
        l_clean = l.strip()
        if not l_clean:
            continue
        traineddata = os.path.join(tessdata_dir, f"{l_clean}.traineddata")
        if os.path.exists(traineddata):
            valid_langs.append(l_clean)
        else:
            missing_langs.append(l_clean)

    if missing_langs:
        raise ValueError(
            f"Requested OCR language model(s) '{'+'.join(missing_langs)}' not installed on worker."
        )

    return "+".join(valid_langs)


class OCREngine:
    """Centralized, hardened OCR execution service for pdfnest-worker."""

    @staticmethod
    def ocr_pixmap(
        pixmap: fitz.Pixmap,
        lang: str = "eng",
        timeout: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """
        Executes Tesseract OCR on a PyMuPDF Pixmap via hardened subprocess.
        Returns list of structured line dictionaries containing 'text', 'bbox', and 'confidence'.
        """
        valid_lang = validate_ocr_language(lang)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_img:
            tmp_img_path = tmp_img.name

        try:
            pixmap.save(tmp_img_path)

            cmd = ["tesseract", tmp_img_path, "stdout", "-l", valid_lang, "tsv"]
            completed = run_hardened_subprocess(cmd, timeout=timeout)

            if completed.returncode != 0:
                logger.error(f"Tesseract OCR process failed with code {completed.returncode}: {completed.stderr}")
                return []

            lines: List[Dict[str, Any]] = []
            tsv_reader = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t")

            line_groups: Dict[tuple[int, int, int], List[Dict[str, Any]]] = {}
            for row in tsv_reader:
                text = row.get("text", "").strip()
                try:
                    conf = int(float(row.get("conf", "-1")))
                except (ValueError, TypeError):
                    conf = -1

                if text and conf > 25:
                    key = (
                        int(row.get("block_num", 0)),
                        int(row.get("par_num", 0)),
                        int(row.get("line_num", 0)),
                    )
                    line_groups.setdefault(key, []).append({
                        "text": text,
                        "left": int(row.get("left", 0)),
                        "top": int(row.get("top", 0)),
                        "width": int(row.get("width", 0)),
                        "height": int(row.get("height", 0)),
                        "conf": conf,
                    })

            for words in line_groups.values():
                if not words:
                    continue
                combined_text = " ".join(w["text"] for w in words)
                x0 = min(w["left"] for w in words)
                y0 = min(w["top"] for w in words)
                x1 = max(w["left"] + w["width"] for w in words)
                y1 = max(w["top"] + w["height"] for w in words)
                avg_conf = sum(w["conf"] for w in words) / len(words)

                lines.append({
                    "text": combined_text,
                    "bbox": (float(x0), float(y0), float(x1), float(y1)),
                    "confidence": float(avg_conf) / 100.0,
                })

            return lines

        finally:
            if os.path.exists(tmp_img_path):
                try:
                    os.remove(tmp_img_path)
                except OSError:
                    pass

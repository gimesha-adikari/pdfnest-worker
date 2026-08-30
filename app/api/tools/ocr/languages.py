from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

LANGUAGE_FILE = Path(__file__).with_name("tesseract_languages.json")


def normalize_tesseract_lang_code(code: str | None) -> str:
    if not code:
        return ""
    return code.strip().lower()


@lru_cache(maxsize=1)
def load_language_names() -> dict[str, str]:
    if not LANGUAGE_FILE.exists():
        return {}

    with LANGUAGE_FILE.open("r", encoding="utf8") as f:
        return json.load(f)


def language_name(code: str) -> str:
    clean_code = normalize_tesseract_lang_code(code)
    return load_language_names().get(clean_code, clean_code)


def get_installed_tesseract_languages() -> tuple[str, ...]:
    # Do not trust a stale TESSDATA_PREFIX blindly.  A directory is only a
    # usable capability root when it contains at least one traineddata file;
    # otherwise continue through the same known system fallbacks used by the
    # OCR V2 adapter.
    candidates = []
    override = os.getenv("TESSDATA_PREFIX", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.extend(Path(path) for path in (
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/share/tessdata",
        "/usr/local/share/tessdata",
    ))
    for tessdata in candidates:
        if not tessdata.is_dir():
            continue
        langs = [p.stem for p in tessdata.glob("*.traineddata") if p.is_file() and p.stem not in {"osd", "pdf"}]
        if langs:
            return tuple(sorted(langs))
    return ()

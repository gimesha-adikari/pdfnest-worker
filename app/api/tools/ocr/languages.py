from __future__ import annotations

import json
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
    from app.api.tools.ocr.document import _get_tessdata_prefix
    tessdata = _get_tessdata_prefix()
    if not tessdata.exists():
        return ("eng",)
    langs = [p.stem for p in tessdata.glob("*.traineddata") if p.is_file()]
    return tuple(sorted(langs)) if langs else ("eng",)
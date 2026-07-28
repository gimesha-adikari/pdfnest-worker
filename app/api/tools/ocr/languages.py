from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .document import normalize_tesseract_lang_code

LANGUAGE_FILE = Path(__file__).with_name("tesseract_languages.json")


@lru_cache(maxsize=1)
def load_language_names() -> dict[str, str]:
    if not LANGUAGE_FILE.exists():
        return {}

    with LANGUAGE_FILE.open("r", encoding="utf8") as f:
        return json.load(f)


def language_name(code: str) -> str:
    clean_code = normalize_tesseract_lang_code(code)
    return load_language_names().get(clean_code, clean_code)
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import pymupdf as fitz
import pytesseract
from PIL import Image, ImageOps

from app.core.storage import download_to_path

_LANG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+-]+$")
_DEFAULT_TESSDATA_PREFIX = "/usr/share/tesseract-ocr/5/tessdata"
_DEFAULT_PSM = "6"
_AUTO_LANG_ALIASES = {"auto", "detect", "auto-detect", "auto detect"}
_OCR_CONFIDENCE_THRESHOLD = 65.0
_OCR_MIN_PREVIEW_DPI = 120
_OCR_MAX_PREVIEW_EDGE = 1600

# Small, practical candidate order for auto mode.
_AUTO_BUNDLE_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("eng", "sin", "tam"),
    ("eng", "sin"),
    ("eng", "tam"),
    ("sin", "tam"),
    ("eng", "hin"),
    ("eng", "ara"),
    ("eng",),
    ("sin",),
    ("tam",),
    ("hin",),
    ("ara",),
    ("rus",),
    ("ell",),
    ("heb",),
    ("ben",),
    ("mya",),
    ("tha",),
    ("chi_sim",),
    ("chi_tra",),
    ("kor",),
    ("jpn",),
)

_SCRIPT_TO_LANGS: dict[str, tuple[str, ...]] = {
    "latin": ("eng",),
    "sinhala": ("sin",),
    "tamil": ("tam",),
    "devanagari": ("hin",),
    "arabic": ("ara",),
    "cyrillic": ("rus",),
    "greek": ("ell",),
    "hebrew": ("heb",),
    "bengali": ("ben",),
    "burmese": ("mya",),
    "thai": ("tha",),
    "hangul": ("kor",),
    "han": ("chi_sim",),
    "hiragana": ("jpn",),
    "katakana": ("jpn",),
}


@dataclass(frozen=True)
class R2ImageRef:
    key: str
    name: str = ""
    content_type: str = ""
    size: int = 0


def _get_tessdata_prefix() -> Path:
    return Path(os.environ.get("TESSDATA_PREFIX", _DEFAULT_TESSDATA_PREFIX)).expanduser()


def _is_auto_lang_spec(lang: str | None) -> bool:
    return bool(lang and lang.strip().lower() in _AUTO_LANG_ALIASES)


def normalize_tesseract_lang_code(raw: str) -> str:
    """
    Convert values like:
      - "afr"
      - "tessdata/afr"
      - "tessdata\\afr"
      - "afr.traineddata"
    into a clean language code:
      - "afr"
    """
    value = (raw or "").strip().replace("\\", "/")
    if not value:
        return value

    if value.endswith(".traineddata"):
        value = Path(value).stem

    if "/" in value:
        value = value.rsplit("/", 1)[-1]

    return value


@lru_cache(maxsize=1)
def get_installed_tesseract_languages() -> set[str]:
    """
    Returns installed Tesseract language codes, normalized.
    """
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            check=True,
            env=os.environ.copy(),
        )
    except Exception:
        return set()

    langs: set[str] = set()
    for line in result.stdout.splitlines():
        line = normalize_tesseract_lang_code(line)
        if not line:
            continue
        if line.lower().startswith("list of available languages"):
            continue
        langs.add(line)

    return langs


def normalize_lang_spec(lang: str | None) -> str:
    """
    Accepts values like:
      "eng"
      "eng+sin"
      "eng, sin, tam"
      " eng + sin + tam "
      "auto"
    and returns a clean Tesseract language spec:
      "eng+sin+tam"
      or "auto"
    """
    if not lang:
        return "eng"

    raw = lang.strip()
    if raw.lower() in _AUTO_LANG_ALIASES:
        return "auto"

    raw = raw.replace(",", "+").replace(" ", "+").strip("+").strip()
    parts = [p.strip() for p in raw.split("+") if p.strip()]

    if not parts:
        return "eng"

    cleaned: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = normalize_tesseract_lang_code(part)
        if part.lower() in _AUTO_LANG_ALIASES:
            return "auto"
        if not _LANG_TOKEN_RE.match(part):
            raise ValueError(
                f"Invalid OCR language token: {part!r}. Use Tesseract language codes like eng, sin, tam, or eng+sin."
            )
        if part not in seen:
            seen.add(part)
            cleaned.append(part)

    return "+".join(cleaned)


def validate_lang_spec(lang: str) -> None:
    """
    If Tesseract is installed, make sure all requested languages exist.
    """
    if _is_auto_lang_spec(lang):
        return

    installed = get_installed_tesseract_languages()
    if not installed:
        return

    requested = [
        normalize_tesseract_lang_code(token)
        for token in lang.split("+")
        if token.strip()
    ]
    missing = [token for token in requested if token and token not in installed]
    if missing:
        raise ValueError(
            "Requested OCR language pack(s) are not installed: "
            + ", ".join(missing)
            + ". Install the matching Tesseract traineddata files or choose from the available packs."
        )


def open_document(input_path: str, password: str | None = None) -> fitz.Document:
    doc = fitz.open(input_path)

    if doc.needs_pass:
        if not password:
            doc.close()
            raise RuntimeError("PDF is password protected but no password was provided")

        if doc.authenticate(password) <= 0:
            doc.close()
            raise RuntimeError("Invalid PDF password")

    return doc


def _render_page_image(page: fitz.Page, dpi: int = 300) -> Image.Image:
    zoom = float(dpi) / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _render_page_preview(page: fitz.Page) -> Image.Image:
    image = _render_page_image(page, dpi=_OCR_MIN_PREVIEW_DPI)
    if max(image.size) <= _OCR_MAX_PREVIEW_EDGE:
        return image

    preview = image.copy()
    preview.thumbnail((_OCR_MAX_PREVIEW_EDGE, _OCR_MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
    return preview


def _load_image_preview(image_path: str) -> Image.Image:
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        if max(img.size) <= _OCR_MAX_PREVIEW_EDGE:
            return img.copy()

        preview = img.copy()
        preview.thumbnail((_OCR_MAX_PREVIEW_EDGE, _OCR_MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
        return preview


def _prepare_image_for_text_ocr(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    return gray


def _detect_scripts_from_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    for ch in text:
        if ch.isspace() or ch.isdigit():
            continue

        script = _script_for_char(ch)
        if script and script not in seen:
            seen.add(script)
            found.append(script)

    return found


def _script_for_char(ch: str) -> str | None:
    code = ord(ch)

    if "A" <= ch <= "Z" or "a" <= ch <= "z":
        return "latin"
    if 0x0D80 <= code <= 0x0DFF:
        return "sinhala"
    if 0x0B80 <= code <= 0x0BFF:
        return "tamil"
    if 0x0900 <= code <= 0x097F:
        return "devanagari"
    if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
        return "arabic"
    if 0x0400 <= code <= 0x04FF or 0x0500 <= code <= 0x052F:
        return "cyrillic"
    if 0x0370 <= code <= 0x03FF:
        return "greek"
    if 0x0590 <= code <= 0x05FF:
        return "hebrew"
    if 0x0980 <= code <= 0x09FF:
        return "bengali"
    if 0x1000 <= code <= 0x109F:
        return "burmese"
    if 0x0E00 <= code <= 0x0E7F:
        return "thai"
    if 0xAC00 <= code <= 0xD7AF:
        return "hangul"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "han"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"

    category = unicodedata.category(ch)
    if category.startswith(("P", "S", "Z", "C")):
        return None

    return None


def _auto_candidate_lang_specs() -> list[str]:
    installed = get_installed_tesseract_languages()
    if not installed:
        return ["eng"]

    candidates: list[str] = []
    seen: set[str] = set()

    def add(spec: str | None) -> None:
        if not spec:
            return
        normalized = normalize_lang_spec(spec)
        if not normalized or normalized == "auto" or normalized in seen:
            return

        parts = [
            normalize_tesseract_lang_code(token)
            for token in normalized.split("+")
            if token.strip()
        ]
        if not parts:
            return
        if any(part not in installed for part in parts):
            return

        seen.add(normalized)
        candidates.append(normalized)

    env_value = os.getenv("OCR_AUTO_FALLBACK_LANGS", "").strip()
    if env_value:
        try:
            add(env_value)
        except Exception:
            pass

    for bundle in _AUTO_BUNDLE_PRIORITY:
        spec = "+".join(code for code in bundle if code in installed)
        add(spec)

    if not candidates:
        if "eng" in installed:
            candidates.append("eng")
        else:
            candidates.append(sorted(installed)[0])

    return candidates


def _lang_spec_from_scripts(scripts: Sequence[str]) -> str:
    installed = get_installed_tesseract_languages()
    if not installed:
        return "eng"

    ordered_codes: list[str] = []
    seen: set[str] = set()

    # Mixed pages and Latin-heavy pages usually work best with English included.
    if ("latin" in scripts or len(scripts) > 1) and "eng" in installed:
        ordered_codes.append("eng")
        seen.add("eng")

    for script in scripts:
        for code in _SCRIPT_TO_LANGS.get(script, ()):
            if code in installed and code not in seen:
                seen.add(code)
                ordered_codes.append(code)

    if ordered_codes:
        return "+".join(ordered_codes)

    return _auto_candidate_lang_specs()[0]


def _ocr_text_with_confidence(image: Image.Image, lang: str) -> tuple[str, float]:
    text = pytesseract.image_to_string(
        image,
        lang=lang,
        config=f"--oem 1 --psm {_DEFAULT_PSM}",
    )

    try:
        data = pytesseract.image_to_data(
            image,
            lang=lang,
            config=f"--oem 1 --psm {_DEFAULT_PSM}",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return text, -1.0

    confs: list[float] = []
    for raw_conf in data.get("conf", []):
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            continue
        if conf >= 0:
            confs.append(conf)

    avg_conf = sum(confs) / len(confs) if confs else -1.0
    return text, avg_conf


def _text_quality_score(text: str) -> float:
    if not text:
        return -50.0

    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    spaces = sum(ch.isspace() for ch in text)
    weird = sum(not ch.isalnum() and not ch.isspace() for ch in text)
    words = len(text.split())

    return (
            letters * 1.0
            + digits * 0.2
            + spaces * 0.05
            + min(words, 40) * 0.8
            - weird * 1.5
    )


def _resolve_auto_lang_spec_from_text_and_image(
        *,
        text_hint: str = "",
        image: Image.Image | None = None,
) -> str:
    """
    Conservative auto mode:
    - If native text exists, infer scripts from that first.
    - Otherwise try a few likely bundles on a preview.
    - Pick the best confidence/quality combo.
    """
    if text_hint.strip():
        scripts = _detect_scripts_from_text(text_hint)
        if scripts:
            return _lang_spec_from_scripts(scripts)

    if image is None:
        return _auto_candidate_lang_specs()[0]

    prepared = _prepare_image_for_text_ocr(image)
    candidates = _auto_candidate_lang_specs()

    best_spec = candidates[0]
    best_score = (float("-inf"), float("-inf"), float("-inf"), float("-inf"))

    for index, spec in enumerate(candidates):
        text, conf = _ocr_text_with_confidence(prepared, spec)
        score = (
            conf if conf >= 0 else -100.0,
            _text_quality_score(text),
            -len(spec.split("+")),
            -index,
        )
        if score > best_score:
            best_score = score
            best_spec = spec

        # If a candidate is clearly good, stop early.
        if score[0] >= 88 and len(text.strip()) >= 40:
            return spec

    return best_spec


def _get_auto_fallback_lang_spec() -> str:
    return _auto_candidate_lang_specs()[0]


def validate_lang_spec(lang: str) -> None:
    """
    If Tesseract is installed, make sure all requested languages exist.
    """
    if _is_auto_lang_spec(lang):
        return

    installed = get_installed_tesseract_languages()
    if not installed:
        return

    requested = [
        normalize_tesseract_lang_code(token)
        for token in lang.split("+")
        if token.strip()
    ]
    missing = [token for token in requested if token and token not in installed]
    if missing:
        raise ValueError(
            "Requested OCR language pack(s) are not installed: "
            + ", ".join(missing)
            + ". Install the matching Tesseract traineddata files or choose from the available packs."
        )


def page_to_ocr_text(page: fitz.Page, lang: str = "eng", dpi: int = 300) -> str:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    image = _render_page_image(page, dpi=dpi)
    prepared = _prepare_image_for_text_ocr(image)

    if _is_auto_lang_spec(lang):
        preview = _render_page_preview(page)
        resolved_lang = _resolve_auto_lang_spec_from_text_and_image(
            text_hint=page.get_text("text") or "",
            image=preview,
        )
        return pytesseract.image_to_string(
            prepared,
            lang=resolved_lang,
            config=f"--oem 1 --psm {_DEFAULT_PSM}",
        )

    return pytesseract.image_to_string(
        prepared,
        lang=lang,
        config=f"--oem 1 --psm {_DEFAULT_PSM}",
    )


def extract_text_from_pdf(
        input_path: str,
        output_path: str,
        lang: str = "eng",
        password: str | None = None,
) -> None:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    doc = open_document(input_path, password=password)

    try:
        with open(output_path, "w", encoding="utf-8") as out:
            for i in range(doc.page_count):
                page = doc[i]

                native_text = (page.get_text("text") or "").strip()
                if native_text:
                    out.write(f"--- START OF PAGE {i + 1} ---\n")
                    out.write(native_text.rstrip() + "\n")
                    out.write("--- END OF PAGE ---\n\n")
                    continue

                ocr_text = page_to_ocr_text(page, lang=lang)
                if ocr_text.strip():
                    out.write(f"--- START OF PAGE {i + 1} ---\n")
                    out.write(ocr_text.rstrip() + "\n")
                    out.write("--- END OF PAGE ---\n\n")
    finally:
        doc.close()


def _image_to_searchable_pdf_bytes(image_path: str, lang: str = "eng") -> bytes:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    tessdata_prefix = _get_tessdata_prefix()

    pdf_config = tessdata_prefix / "configs" / "pdf"
    pdf_font = tessdata_prefix / "pdf.ttf"
    if not pdf_config.is_file():
        raise RuntimeError(
            f"Missing Tesseract PDF config: {pdf_config}. "
            "Your tessdata directory must include configs/pdf."
        )
    if not pdf_font.is_file():
        raise RuntimeError(
            f"Missing Tesseract PDF font: {pdf_font}. "
            "Your tessdata directory must include pdf.ttf."
        )

    with tempfile.TemporaryDirectory(prefix="pdfnest-ocr-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_file = tmpdir_path / "input.png"
        output_base = tmpdir_path / "ocr-output"

        with Image.open(image_path) as img:
            prepared = ImageOps.exif_transpose(img).convert("RGB")
            prepared.save(input_file, "PNG", dpi=(300, 300))

        resolved_lang = lang
        if _is_auto_lang_spec(lang):
            preview = _load_image_preview(image_path)
            resolved_lang = _resolve_auto_lang_spec_from_text_and_image(
                text_hint="",
                image=preview,
            )

        cmd = [
            "tesseract",
            str(input_file),
            str(output_base),
            "-l",
            resolved_lang,
            "--oem",
            "1",
            "--psm",
            _DEFAULT_PSM,
            "pdf",
        ]

        env = os.environ.copy()
        env.setdefault("TESSDATA_PREFIX", str(tessdata_prefix))

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=str(tmpdir_path),
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Tesseract OCR execution timed out after 5 minutes") from exc

        pdf_path = output_base.with_suffix(".pdf")

        if result.returncode != 0:
            raise RuntimeError(
                f"Tesseract PDF generation failed.\n"
                f"Return code: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )

        if not pdf_path.exists():
            raise RuntimeError(
                f"Tesseract did not create expected PDF output: {pdf_path}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )

        return pdf_path.read_bytes()


def build_searchable_pdf_from_images(
        image_paths: Sequence[str],
        output_path: str,
        lang: str = "eng",
) -> None:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if not image_paths:
        raise ValueError("no images provided")

    out_doc = fitz.open()

    try:
        for img_path in image_paths:
            if not Path(img_path).exists():
                raise FileNotFoundError(f"image not found: {img_path}")

            pdf_bytes = _image_to_searchable_pdf_bytes(img_path, lang=lang)
            page_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

            try:
                out_doc.insert_pdf(page_doc)
            finally:
                page_doc.close()

        out_doc.save(output_path, garbage=4, clean=True, deflate=True)
    finally:
        out_doc.close()


def build_searchable_pdf_from_r2_images(
        image_refs: Sequence[R2ImageRef],
        output_path: str,
        lang: str = "eng",
) -> None:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if not image_refs:
        raise ValueError("no images provided")

    with tempfile.TemporaryDirectory(prefix="pdfnest-ocr-r2-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        temp_paths: list[str] = []

        for index, ref in enumerate(image_refs, start=1):
            key = (ref.key or "").strip()
            if not key:
                raise ValueError(f"missing R2 object key for image #{index}")

            suffix = safe_suffix(ref.name or ref.key, ".img")
            local_path = tmpdir_path / f"image-{index:03d}{suffix}"

            download_to_path(key, str(local_path))
            temp_paths.append(str(local_path))

        build_searchable_pdf_from_images(temp_paths, output_path, lang=lang)


def safe_suffix(filename: str | None, fallback: str = ".bin") -> str:
    if not filename:
        return fallback

    suffix = Path(filename).suffix
    return suffix if suffix else fallback
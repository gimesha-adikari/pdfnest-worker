from __future__ import annotations

import os
import re
import subprocess
import tempfile
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
_DEFAULT_PSM = "6"  # Better for dense printed pages than psm 1


@dataclass(frozen=True)
class R2ImageRef:
    key: str
    name: str = ""
    content_type: str = ""
    size: int = 0


def _get_tessdata_prefix() -> Path:
    return Path(os.environ.get("TESSDATA_PREFIX", _DEFAULT_TESSDATA_PREFIX)).expanduser()


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
    and returns a clean Tesseract language spec:
      "eng+sin+tam"
    """
    if not lang:
        return "eng"

    raw = lang.replace(",", "+").replace(" ", "+").strip("+").strip()
    parts = [p.strip() for p in raw.split("+") if p.strip()]

    if not parts:
        return "eng"

    cleaned: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = normalize_tesseract_lang_code(part)
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


def page_to_ocr_text(page: fitz.Page, lang: str = "eng", dpi: int = 300) -> str:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    zoom = float(dpi) / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)

    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if image.mode != "RGB":
        image = image.convert("RGB")

    return pytesseract.image_to_string(
        image,
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
    """
    Generate searchable PDF bytes using the Tesseract CLI.

    This expects a complete tessdata directory that includes:
      - *.traineddata
      - configs/pdf
      - pdf.ttf
    """
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
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.save(input_file, "PNG", dpi=(300, 300))

        cmd = [
            "tesseract",
            str(input_file),
            str(output_base),
            "-l",
            lang,
            "--oem",
            "1",
            "--psm",
            _DEFAULT_PSM,
            "pdf",
        ]

        env = os.environ.copy()
        env.setdefault("TESSDATA_PREFIX", str(tessdata_prefix))

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(tmpdir_path),
        )

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
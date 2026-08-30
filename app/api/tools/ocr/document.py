from __future__ import annotations

import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Sequence

import pymupdf as fitz
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

from app.api.tools.ocr.languages import (
    get_installed_tesseract_languages,
    normalize_tesseract_lang_code,
)
from app.api.tools.ocr.schemas import R2ImageRef
from app.core.storage import download_to_path
from app.core.subprocess_runner import run_hardened_subprocess
from app.core.tesseract_capacity import acquire_tesseract_capacity

logger = logging.getLogger(__name__)

_DEFAULT_PSM = "6"
_OCR_CONFIDENCE_THRESHOLD = 65.0
_OCR_MIN_PREVIEW_DPI = 120
_OCR_MAX_PREVIEW_EDGE = 1600

# Maximum number of candidate language bundles to try during auto-detection.
# Each candidate spawns a Tesseract process, so this directly caps the
# subprocess multiplier for auto-mode OCR.
_OCR_MAX_AUTO_CANDIDATES = int(os.environ.get("OCR_MAX_AUTO_CANDIDATES", "5"))

# Maximum concurrent page workers per document. The Global Tesseract Capacity Governor
# (app/core/tesseract_capacity.py) strictly enforces MAX 2 active Tesseract processes globally.
_OCR_PAGE_WORKERS = int(os.environ.get("OCR_PAGE_WORKERS", "2"))

# Prefer likely local language bundles before broader OCR fallback candidates.
_AUTO_BUNDLE_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("eng", "sin", "tam"),
    ("eng", "sin"),
    ("eng", "tam"),
    ("sin", "tam"),
    ("eng", "deu", "fra", "spa"),
    ("eng", "deu"),
    ("eng", "fra"),
    ("eng", "spa"),
    ("eng", "ita"),
    ("eng", "por"),
    ("eng", "nld"),
    ("eng", "rus"),
    ("eng", "ara"),
    ("eng", "hin"),
    ("eng", "ben"),
    ("eng", "chi_sim", "chi_tra"),
    ("eng", "chi_sim"),
    ("eng", "chi_tra"),
    ("eng", "jpn"),
    ("eng", "kor"),
)

_SCRIPT_TO_LANGS: dict[str, tuple[str, ...]] = {
    "SINHALA": ("sin", "eng"),
    "TAMIL": ("tam", "eng"),
    "LATIN": ("eng", "deu", "fra", "spa"),
    "DEVANAGARI": ("hin", "eng"),
    "BENGALI": ("ben", "eng"),
    "CYRILLIC": ("rus", "eng"),
    "ARABIC": ("ara", "eng"),
    "HAN": ("chi_sim", "chi_tra", "eng"),
    "HIRAGANA": ("jpn", "eng"),
    "KATAKANA": ("jpn", "eng"),
    "HANGUL": ("kor", "eng"),
}


def open_document(input_path: str, password: str | None = None) -> fitz.Document:
    if not Path(input_path).exists():
        raise FileNotFoundError(f"PDF file not found: {input_path}")

    try:
        doc = fitz.open(input_path)
    except Exception as exc:
        raise ValueError(f"Failed to open PDF document: {exc}") from exc

    if doc.is_encrypted:
        if not password:
            doc.close()
            raise ValueError("PDF is password-protected. Password is required.")

        success = doc.authenticate(password)
        if not success:
            doc.close()
            raise ValueError("Invalid password provided for encrypted PDF.")

    return doc


def normalize_lang_spec(lang: str | None) -> str:
    if not lang or not lang.strip():
        return "eng"

    cleaned = lang.strip().lower()
    if cleaned in ("auto", "automatic", "detect"):
        return "auto"

    raw_tokens = [t.strip() for t in re.split(r"[\s,+]|%2b|%20", cleaned) if t.strip()]
    normalized: list[str] = []

    for token in raw_tokens:
        code = normalize_tesseract_lang_code(token)
        if code and code not in normalized:
            normalized.append(code)

    if not normalized:
        return "eng"

    return "+".join(normalized)


def _is_auto_lang_spec(lang: str) -> bool:
    return normalize_lang_spec(lang) == "auto"


@lru_cache(maxsize=1)
def _get_tessdata_prefix() -> Path:
    prefix_env = os.environ.get("TESSDATA_PREFIX")
    if prefix_env:
        p = Path(prefix_env)
        if p.exists():
            return p

    candidates = [
        Path("/usr/share/tesseract-ocr/5/tessdata"),
        Path("/usr/share/tesseract-ocr/4.00/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/usr/local/share/tessdata"),
    ]
    for c in candidates:
        if c.exists():
            return c

    return Path("/usr/share/tesseract-ocr/5/tessdata")


def is_tesseract_available() -> bool:
    tessdata_prefix = _get_tessdata_prefix()
    return (tessdata_prefix / "eng.traineddata").is_file()


def _render_page_image(page: fitz.Page, dpi: int = 300) -> Image.Image:
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _render_page_preview(page: fitz.Page) -> Image.Image:
    image = _render_page_image(page, dpi=_OCR_MIN_PREVIEW_DPI)
    if max(image.size) <= _OCR_MAX_PREVIEW_EDGE:
        return image

    image.thumbnail((_OCR_MAX_PREVIEW_EDGE, _OCR_MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
    return image


def _load_image_preview(image_path: str) -> Image.Image:
    with Image.open(image_path) as img:
        preview = ImageOps.exif_transpose(img).convert("RGB")
        if max(preview.size) <= _OCR_MAX_PREVIEW_EDGE:
            return preview.copy()

        preview.thumbnail((_OCR_MAX_PREVIEW_EDGE, _OCR_MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
        return preview.copy()


def _prepare_image_for_text_ocr(image: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(image).convert("RGB")

    gray = ImageOps.grayscale(img)
    stat = ImageStat.Stat(gray)
    mean_val = stat.mean[0] if stat.mean else 128.0
    std_val = stat.stddev[0] if stat.stddev else 50.0

    if std_val < 35.0:
        gray = ImageEnhance.Contrast(gray).enhance(1.8)

    if mean_val < 100.0:
        gray = ImageOps.autocontrast(gray, cutoff=1)

    return gray


@lru_cache(maxsize=1)
def _auto_candidate_lang_specs() -> tuple[str, ...]:
    installed = set(get_installed_tesseract_languages())
    specs: list[str] = []

    for bundle in _AUTO_BUNDLE_PRIORITY:
        available = [code for code in bundle if code in installed]
        if available:
            spec = "+".join(available)
            if spec not in specs:
                specs.append(spec)

    if not specs and "eng" in installed:
        specs.append("eng")

    if not specs and installed:
        specs.append(sorted(installed)[0])

    return tuple(specs)


def _detect_scripts_from_text(text: str) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isprintable() or ch.isspace() or ch.isdigit():
            continue
        try:
            script = unicodedata.name(ch).split()[0]
        except (ValueError, IndexError):
            continue

        counts[script] = counts.get(script, 0) + 1

    sorted_scripts = sorted(counts.keys(), key=lambda s: counts[s], reverse=True)
    return tuple(sorted_scripts)


def _lang_spec_from_scripts(scripts: Sequence[str]) -> str:
    installed = set(get_installed_tesseract_languages())
    ordered_codes: list[str] = []
    seen: set[str] = set()

    for script in scripts:
        for code in _SCRIPT_TO_LANGS.get(script, ()):
            if code in installed and code not in seen:
                seen.add(code)
                ordered_codes.append(code)

    if ordered_codes:
        return "+".join(ordered_codes)

    return _auto_candidate_lang_specs()[0]


def run_hardened_tesseract_ocr(
    image: Image.Image | str,
    lang: str = "eng",
    psm: str = _DEFAULT_PSM,
    output_format: str = "txt",
    cancellation_check: Callable[[], None] | None = None,
    timeout: float = 300.0,
) -> str | tuple[str, float]:
    """
    Executes Tesseract CLI via run_hardened_subprocess in an isolated process group.
    Acquires global Tesseract capacity token (acquire_tesseract_capacity) to ensure
    active concurrent Tesseract processes never exceed GLOBAL_TESSERACT_CAPACITY across
    the entire Python worker.
    """
    tessdata_prefix = _get_tessdata_prefix()
    env = os.environ.copy()
    env.setdefault("TESSDATA_PREFIX", str(tessdata_prefix))

    with tempfile.TemporaryDirectory(prefix="pdfnest-ocr-tess-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        output_base = tmpdir_path / "tess_out"

        if isinstance(image, str):
            input_file_path = image
        else:
            input_file_path = str(tmpdir_path / "input.png")
            prepared_img = ImageOps.exif_transpose(image).convert("RGB")
            prepared_img.save(input_file_path, "PNG", dpi=(300, 300))

        cmd = [
            "tesseract",
            input_file_path,
            str(output_base),
            "-l",
            lang,
            "--oem",
            "1",
            "--psm",
            str(psm),
            output_format,
        ]

        logger.info("[OCR SUBPROCESS] Starting Tesseract CLI (format: %s, lang: %s)...", output_format, lang)

        # Acquire global capacity token to protect CPU/RAM invariant (active Tesseract processes <= 2)
        with acquire_tesseract_capacity(cancellation_check=cancellation_check, timeout=timeout):
            result = run_hardened_subprocess(
                cmd,
                env=env,
                cwd=str(tmpdir_path),
                cancellation_check=cancellation_check,
                timeout=timeout,
            )

        if result.returncode != 0:
            logger.error("[OCR SUBPROCESS] Tesseract failed with returncode %d: %s", result.returncode, result.stderr)
            raise RuntimeError(f"Tesseract OCR failed ({result.returncode}): {result.stderr}")

        if output_format == "txt":
            out_file = output_base.with_suffix(".txt")
            if not out_file.exists():
                return ""
            return out_file.read_text(encoding="utf-8", errors="replace")

        elif output_format == "tsv":
            out_file = output_base.with_suffix(".tsv")
            if not out_file.exists():
                return "", -1.0

            content = out_file.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if not lines:
                return "", -1.0

            text_words: list[str] = []
            confs: list[float] = []

            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) >= 12:
                    raw_conf = parts[10]
                    word_text = parts[11].strip()
                    try:
                        conf = float(raw_conf)
                        if conf >= 0:
                            confs.append(conf)
                            if word_text:
                                text_words.append(word_text)
                    except ValueError:
                        pass

            extracted_text = " ".join(text_words)
            avg_conf = (sum(confs) / len(confs)) if confs else -1.0
            return extracted_text, avg_conf

        else:
            raise ValueError(f"Unsupported output format: {output_format}")


def _ocr_text_with_confidence(
    image: Image.Image,
    lang: str,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[str, float]:
    result = run_hardened_tesseract_ocr(
        image,
        lang=lang,
        psm=_DEFAULT_PSM,
        output_format="tsv",
        cancellation_check=cancellation_check,
    )
    if isinstance(result, tuple):
        return result
    return str(result), -1.0


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
    cancellation_check: Callable[[], None] | None = None,
) -> str:
    if text_hint.strip():
        scripts = _detect_scripts_from_text(text_hint)
        if scripts:
            return _lang_spec_from_scripts(scripts)

    if image is None:
        return _auto_candidate_lang_specs()[0]

    prepared = _prepare_image_for_text_ocr(image)
    candidates = _auto_candidate_lang_specs()
    candidates = candidates[:_OCR_MAX_AUTO_CANDIDATES]

    best_spec = candidates[0]
    best_score = (float("-inf"), float("-inf"), float("-inf"), float("-inf"))

    for index, spec in enumerate(candidates):
        if cancellation_check:
            cancellation_check()
        text, conf = _ocr_text_with_confidence(prepared, spec, cancellation_check=cancellation_check)
        score = (
            conf if conf >= 0 else -100.0,
            _text_quality_score(text),
            -len(spec.split("+")),
            -index,
        )
        if score > best_score:
            best_score = score
            best_spec = spec

        if score[0] >= 88 and len(text.strip()) >= 40:
            return spec

    return best_spec


def _get_auto_fallback_lang_spec() -> str:
    return _auto_candidate_lang_specs()[0]


def validate_lang_spec(lang: str) -> None:
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


def page_to_ocr_text(
    page: fitz.Page,
    lang: str = "eng",
    dpi: int = 300,
    cancellation_check: Callable[[], None] | None = None,
) -> str:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if cancellation_check:
        cancellation_check()

    image = _render_page_image(page, dpi=dpi)

    if cancellation_check:
        cancellation_check()

    prepared = _prepare_image_for_text_ocr(image)

    if cancellation_check:
        cancellation_check()

    if _is_auto_lang_spec(lang):
        preview = _render_page_preview(page)
        if cancellation_check:
            cancellation_check()
        resolved_lang = _resolve_auto_lang_spec_from_text_and_image(
            text_hint=page.get_text("text") or "",
            image=preview,
            cancellation_check=cancellation_check,
        )
        res = run_hardened_tesseract_ocr(
            prepared,
            lang=resolved_lang,
            psm=_DEFAULT_PSM,
            output_format="txt",
            cancellation_check=cancellation_check,
        )
        return str(res)

    res = run_hardened_tesseract_ocr(
        prepared,
        lang=lang,
        psm=_DEFAULT_PSM,
        output_format="txt",
        cancellation_check=cancellation_check,
    )
    return str(res)


def _process_page_for_text_extraction(
    input_path: str,
    page_num: int,
    password: str | None,
    lang: str,
    cancellation_check: Callable[[], None] | None,
) -> tuple[int, str]:
    """Helper for page worker tasks. Opens document independently per worker thread."""
    if cancellation_check:
        cancellation_check()

    doc = open_document(input_path, password=password)
    try:
        if cancellation_check:
            cancellation_check()

        page = doc[page_num]
        native_text = (page.get_text("text") or "").strip()
        if native_text:
            return page_num, native_text

        if cancellation_check:
            cancellation_check()

        ocr_text = page_to_ocr_text(page, lang=lang, cancellation_check=cancellation_check)
        return page_num, ocr_text
    finally:
        doc.close()


def extract_text_from_pdf(
    input_path: str,
    output_path: str,
    lang: str = "eng",
    password: str | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    """Extract text through the OCR V2 native-first compatibility adapter.

    The legacy endpoint and its durable task contract remain unchanged.  For
    explicit languages, the implementation now consumes canonical OCR V2 page
    results so native pages avoid OCR and scanned pages use the shared
    Tesseract TSV adapter.  Automatic-language mode remains the V1-compatible
    path because OCR V2 intentionally requires explicit languages.
    """
    if cancellation_check:
        cancellation_check()

    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if not _is_auto_lang_spec(lang):
        _extract_text_from_pdf_v2(
            input_path,
            output_path,
            lang,
            password=password,
            cancellation_check=cancellation_check,
        )
        return

    doc = open_document(input_path, password=password)
    total_pages = doc.page_count
    doc.close()

    if total_pages <= 0:
        with open(output_path, "w", encoding="utf-8") as out:
            pass
        return

    page_results: list[str | None] = [None] * total_pages

    if total_pages == 1 or _OCR_PAGE_WORKERS <= 1:
        for i in range(total_pages):
            if cancellation_check:
                cancellation_check()
            _, text = _process_page_for_text_extraction(input_path, i, password, lang, cancellation_check)
            page_results[i] = text
    else:
        executor = ThreadPoolExecutor(max_workers=_OCR_PAGE_WORKERS)
        futures: dict[Any, int] = {}
        try:
            for i in range(total_pages):
                if cancellation_check:
                    cancellation_check()
                fut = executor.submit(
                    _process_page_for_text_extraction,
                    input_path,
                    i,
                    password,
                    lang,
                    cancellation_check,
                )
                futures[fut] = i

            for future in as_completed(futures):
                if cancellation_check:
                    cancellation_check()
                idx, text = future.result()
                page_results[idx] = text
        except Exception:
            for fut in futures:
                fut.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    if cancellation_check:
        cancellation_check()

    try:
        with open(output_path, "w", encoding="utf-8") as out:
            for i, text in enumerate(page_results):
                if cancellation_check:
                    cancellation_check()
                if text and text.strip():
                    out.write(f"--- START OF PAGE {i + 1} ---\n")
                    out.write(text.rstrip() + "\n")
                    out.write("--- END OF PAGE ---\n\n")
    except Exception:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise


def _extract_text_from_pdf_v2(
    input_path: str,
    output_path: str,
    lang: str,
    *,
    password: str | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    """Write the legacy text-file shape from canonical OCR V2 pages."""
    from app.core.ocr_v2.orchestration import OCRV2Worker
    from app.core.ocr_v2.validation import OCRProfile

    result = OCRV2Worker().process_document(
        input_path,
        password=password,
        language=lang,
        profile=OCRProfile.OCR_TEXT_V2,
        cancellation_check=cancellation_check,
    )
    failed_pages = [page.page_index for page in result.pages if page.status.value == "FAILED"]
    if failed_pages:
        raise RuntimeError(f"OCR V2 failed on page(s): {', '.join(str(index + 1) for index in failed_pages)}")

    try:
        with open(output_path, "w", encoding="utf-8") as out:
            for index, page in enumerate(result.pages):
                if cancellation_check:
                    cancellation_check()
                if page.text and page.text.strip():
                    out.write(f"--- START OF PAGE {index + 1} ---\n")
                    out.write(page.text.rstrip() + "\n")
                    out.write("--- END OF PAGE ---\n\n")
    except Exception:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise


def _image_to_searchable_pdf_bytes(
    image_path: str,
    lang: str = "eng",
    cancellation_check: Callable[[], None] | None = None,
) -> bytes:
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
                cancellation_check=cancellation_check,
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
            with acquire_tesseract_capacity(cancellation_check=cancellation_check, timeout=300.0):
                result = run_hardened_subprocess(
                    cmd,
                    env=env,
                    cwd=str(tmpdir_path),
                    cancellation_check=cancellation_check,
                    timeout=300.0,
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


def _process_single_image_to_pdf_bytes(
    img_path: str,
    index: int,
    lang: str,
    cancellation_check: Callable[[], None] | None,
) -> tuple[int, bytes]:
    if cancellation_check:
        cancellation_check()
    if not Path(img_path).exists():
        raise FileNotFoundError(f"image not found: {img_path}")

    pdf_bytes = _image_to_searchable_pdf_bytes(img_path, lang=lang, cancellation_check=cancellation_check)
    return index, pdf_bytes


def build_searchable_pdf_from_images(
    image_paths: Sequence[str],
    output_path: str,
    lang: str = "eng",
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if not image_paths:
        raise ValueError("no images provided")

    total_images = len(image_paths)
    pdf_bytes_list: list[bytes | None] = [None] * total_images

    if total_images == 1 or _OCR_PAGE_WORKERS <= 1:
        for idx, img_path in enumerate(image_paths):
            _, b = _process_single_image_to_pdf_bytes(img_path, idx, lang, cancellation_check)
            pdf_bytes_list[idx] = b
    else:
        with ThreadPoolExecutor(max_workers=_OCR_PAGE_WORKERS) as executor:
            futures = {
                executor.submit(_process_single_image_to_pdf_bytes, img_path, idx, lang, cancellation_check): idx
                for idx, img_path in enumerate(image_paths)
            }
            for future in as_completed(futures):
                idx, b = future.result()
                pdf_bytes_list[idx] = b

    out_doc = fitz.open()
    try:
        for pdf_bytes in pdf_bytes_list:
            if pdf_bytes:
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
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    lang = normalize_lang_spec(lang)
    validate_lang_spec(lang)

    if not image_refs:
        raise ValueError("no images provided")

    with tempfile.TemporaryDirectory(prefix="pdfnest-ocr-r2-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        temp_paths: list[str] = []

        for index, ref in enumerate(image_refs, start=1):
            if cancellation_check:
                cancellation_check()
            key = (ref.key or "").strip()
            if not key:
                raise ValueError(f"missing R2 object key for image #{index}")

            suffix = safe_suffix(ref.name or ref.key, ".img")
            local_path = tmpdir_path / f"image-{index:03d}{suffix}"

            download_to_path(key, str(local_path))
            temp_paths.append(str(local_path))

        build_searchable_pdf_from_images(temp_paths, output_path, lang=lang, cancellation_check=cancellation_check)


def safe_suffix(filename: str | None, fallback: str = ".bin") -> str:
    if not filename:
        return fallback

    ext = Path(filename).suffix.lower()
    if ext in (
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
        ".txt",
        ".json",
    ):
        return ext

    guess = mimetypes.guess_extension(filename)
    if guess:
        return guess

    return fallback

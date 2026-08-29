"""Deterministic image-input page preparation for Searchable PDF V2.

Images are normalized once, then the same normalized raster is embedded in the
source PDF that is both OCR'd and rendered.  This keeps visible appearance and
OCR coordinates on the same page geometry and makes EXIF orientation harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

import pymupdf as fitz
from PIL import Image, ImageOps


# Phase 2H established 150 DPI as the canonical benchmark raster policy.  The
# input image's normalized pixel dimensions determine the page's physical size
# at that fixed scale, preserving aspect ratio without distortion.
DEFAULT_DPI = 150
SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


@dataclass(frozen=True)
class NormalizedImage:
    source_path: str
    format: str
    width: int
    height: int
    png_bytes: bytes
    page_width: float
    page_height: float
    dpi: int = DEFAULT_DPI


def normalize_image(path: str | Path, *, dpi: int = DEFAULT_DPI) -> NormalizedImage:
    source = Path(path)
    with Image.open(source) as opened:
        image_format = str(opened.format or "").upper()
        if image_format not in SUPPORTED_FORMATS:
            raise ValueError(f"unsupported image format: {image_format or 'unknown'}")
        opened.load()
        image = ImageOps.exif_transpose(opened)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        buffer = BytesIO()
        image.save(buffer, format="PNG", dpi=(dpi, dpi))
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    return NormalizedImage(
        source_path=str(source),
        format=image_format,
        width=width,
        height=height,
        png_bytes=buffer.getvalue(),
        page_width=max(1.0, width * 72.0 / dpi),
        page_height=max(1.0, height * 72.0 / dpi),
        dpi=dpi,
    )


def build_image_source_pdf(
    image_paths: Iterable[str | Path],
    output_pdf: str | Path,
    *,
    dpi: int = DEFAULT_DPI,
) -> tuple[NormalizedImage, ...]:
    """Build an ordered, image-only PDF without changing input order."""

    normalized = tuple(normalize_image(path, dpi=dpi) for path in image_paths)
    if not normalized:
        raise ValueError("at least one image is required")
    target = Path(output_pdf)
    target.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        for image in normalized:
            page = document.new_page(width=image.page_width, height=image.page_height)
            page.insert_image(page.rect, stream=image.png_bytes, keep_proportion=False, overlay=False)
        document.save(str(target), garbage=3, deflate=True)
    return normalized

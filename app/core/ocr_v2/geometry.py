"""Canonical PDF-page geometry and raster preparation.

All canonical rectangles use visible CropBox-relative PDF points, top-left
origin, X right and Y down.  Raster dimensions come from the raster itself;
no nominal DPI is used to infer its size.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pymupdf as fitz
from PIL import Image, ImageOps

from .contracts import PageGeometry, Rect


def normalize_rotation(rotation: int) -> int:
    value = int(rotation) % 360
    if value not in (0, 90, 180, 270):
        raise ValueError(f"unsupported PDF rotation: {rotation}")
    return value


def page_geometry_from_pdf(page: Any, *, pixel_width: int | None = None, pixel_height: int | None = None) -> PageGeometry:
    """Read the visible, rotated page rect exposed by PyMuPDF."""

    rect = page.rect
    return PageGeometry(
        width=float(rect.width),
        height=float(rect.height),
        rotation=normalize_rotation(getattr(page, "rotation", 0)),
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )


def pixel_rect_to_points(box: tuple[float, float, float, float], geometry: PageGeometry) -> Rect:
    """Convert an x0/y0/x1/y1 raster box using actual raster dimensions."""

    if not geometry.pixel_width or not geometry.pixel_height:
        raise ValueError("pixel dimensions are required for raster geometry conversion")
    x0, y0, x1, y1 = (float(value) for value in box)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("raster box must have positive area")
    return Rect(
        x=x0 * geometry.width / geometry.pixel_width,
        y=y0 * geometry.height / geometry.pixel_height,
        width=(x1 - x0) * geometry.width / geometry.pixel_width,
        height=(y1 - y0) * geometry.height / geometry.pixel_height,
    )


def clamp_rect(rect: Rect, geometry: PageGeometry) -> Rect:
    x0 = max(0.0, min(geometry.width, rect.x))
    y0 = max(0.0, min(geometry.height, rect.y))
    x1 = max(x0, min(geometry.width, rect.x1))
    y1 = max(y0, min(geometry.height, rect.y1))
    return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class PreparedRaster:
    image: Image.Image
    png_bytes: bytes
    geometry: PageGeometry
    dpi: int


class RasterPreparer:
    """Render a PDF page and normalize EXIF orientation before OCR."""

    def __init__(self, dpi: int = 200) -> None:
        if dpi <= 0:
            raise ValueError("dpi must be positive")
        self.dpi = dpi

    def prepare(self, page: Any) -> PreparedRaster:
        scale = self.dpi / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        raw = pixmap.tobytes("png")
        with Image.open(io.BytesIO(raw)) as loaded:
            image = ImageOps.exif_transpose(loaded).convert("RGB")
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", dpi=(self.dpi, self.dpi))
        geometry = page_geometry_from_pdf(page, pixel_width=image.width, pixel_height=image.height)
        return PreparedRaster(image=image, png_bytes=encoded.getvalue(), geometry=geometry, dpi=self.dpi)

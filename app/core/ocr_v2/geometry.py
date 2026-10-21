"""Canonical PDF-page geometry and raster preparation.

All canonical rectangles use visible CropBox-relative PDF points, top-left
origin, X right and Y down.  Raster dimensions come from the raster itself;
no nominal DPI is used to infer its size.
"""

from __future__ import annotations

import io
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from numbers import Real
from typing import Any, Callable

import pymupdf as fitz
from PIL import Image, ImageOps

from .contracts import PageGeometry, Rect
from .errors import ConfigurationError, RasterAdmissionError


HARD_MAX_RASTER_PIXELS = 10_000_000
MAX_RASTER_DPI = 600
MAX_RASTER_AXIS = 32_768
# Bound coordinate precision before MuPDF's float32/integer conversions.
MAX_RASTER_COORDINATE = 1_048_576
_raster_context: ContextVar[tuple[int | None, Callable[[], None] | None]] = ContextVar(
    "raster_policy", default=(None, None),
)


def validate_raster_dpi(dpi: int) -> int:
    if type(dpi) is not int or not 1 <= dpi <= MAX_RASTER_DPI:
        raise ConfigurationError(f"raster DPI must be an integer from 1 to {MAX_RASTER_DPI}")
    return dpi


def effective_raster_limit(*limits: int | None) -> int:
    result = HARD_MAX_RASTER_PIXELS
    for value in limits:
        if value is None:
            continue
        if type(value) is not int or value <= 0:
            raise ConfigurationError("raster pixel limit must be a positive integer")
        result = min(result, value)
    return result


def raster_env_int(name: str, default: int | None = None) -> int | None:
    """Missing is optional; an explicitly malformed value is a configuration error."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    effective_raster_limit(value)
    return value


@contextmanager
def raster_policy(limit: int | None, cancellation_check: Callable[[], None] | None = None) -> Iterator[None]:
    """Carry worker policy through existing prepare(page) adapters without API churn."""
    outer_limit, outer_cancel = _raster_context.get()
    token = _raster_context.set((effective_raster_limit(outer_limit, limit), cancellation_check or outer_cancel))
    try:
        yield
    finally:
        _raster_context.reset(token)


@dataclass(frozen=True)
class RasterAdmission:
    """Conservative upper dimensions, not observed pixel dimensions."""

    width: int
    height: int
    limit: int
    matrix: Any
    clip: Any = None


def _bounds(rect: Any) -> tuple[float, float, float, float]:
    try:
        raw = (rect.x0, rect.y0, rect.x1, rect.y1)
        if any(isinstance(v, bool) or not isinstance(v, Real) for v in raw):
            raise ValueError("non-numeric coordinates")
        bounds = tuple(float(v) for v in raw)
        if any(not math.isfinite(v) or abs(v) > MAX_RASTER_COORDINATE for v in bounds):
            raise ValueError("nonfinite or extreme coordinates")
        x0, y0, x1, y1 = bounds
        if x1 <= x0 or y1 <= y0:
            raise ValueError("unordered or empty rectangle")
        return bounds
    except (AttributeError, TypeError, ValueError, OverflowError, RuntimeError) as exc:
        raise RasterAdmissionError("invalid raster page/clip geometry") from exc


def _check_dimensions(width: int, height: int, limit: int) -> None:
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width > MAX_RASTER_AXIS or height > MAX_RASTER_AXIS
            or height > limit // width):
        raise RasterAdmissionError("raster dimensions exceed the security budget")


def estimate_raster(
    page: Any, *, dpi: int = 200, max_raster_pixels: int | None = None,
    clip: Any = None, matrix: Any = None,
) -> RasterAdmission:
    """Admit the visible rotated rect; never rotate page.rect a second time.

    Supported rendering is an unshifted uniform DPI matrix, optionally clipped
    in visible page coordinates. Floor/ceil endpoints outwards and add one
    pixel on each edge: at bounded coordinates this exceeds float32 error and
    MuPDF's 1e-3 rounding tolerance. Never use Rect.irect's tolerant rounding
    as an upper bound. No PDF contents are decoded here.
    """
    validate_raster_dpi(dpi)
    limit = effective_raster_limit(_raster_context.get()[0], max_raster_pixels)
    try:
        rect = page.rect
        rotation = getattr(page, "rotation", 0)
    except (AttributeError, ValueError, RuntimeError) as exc:
        raise RasterAdmissionError("raster page geometry is unavailable") from exc
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise RasterAdmissionError("invalid raster page rotation")
    x0, y0, x1, y1 = _bounds(rect)
    if clip is not None:
        cx0, cy0, cx1, cy1 = _bounds(clip)
        x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
        if x1 <= x0 or y1 <= y0:
            raise RasterAdmissionError("raster clip does not intersect the visible page")
        clip = fitz.Rect(x0, y0, x1, y1)
    scale = dpi / 72.0
    expected = (scale, 0.0, 0.0, scale, 0.0, 0.0)
    if matrix is not None:
        try:
            values = tuple(matrix)
            if (any(isinstance(v, bool) or not isinstance(v, Real) for v in values)
                    or values != expected):
                raise ValueError("unsupported transform")
        except (TypeError, ValueError, OverflowError) as exc:
            raise RasterAdmissionError("invalid raster transformation") from exc
    matrix = fitz.Matrix(scale, scale)
    edges = (x0 * matrix.a, y0 * matrix.d, x1 * matrix.a, y1 * matrix.d)
    if any(not math.isfinite(v) or abs(v) > MAX_RASTER_COORDINATE for v in edges):
        raise RasterAdmissionError("raster transformation exceeds coordinate bounds")
    width = math.ceil(edges[2]) - math.floor(edges[0]) + 2
    height = math.ceil(edges[3]) - math.floor(edges[1]) + 2
    _check_dimensions(width, height, limit)
    return RasterAdmission(width, height, limit, matrix, clip)


def render_page_pixmap(
    page: Any, *, dpi: int = 200, max_raster_pixels: int | None = None,
    clip: Any = None, cancellation_check: Callable[[], None] | None = None,
) -> fitz.Pixmap:
    """The shared OCR native raster sink; check before and immediately after it."""
    cancel = cancellation_check or _raster_context.get()[1]
    if cancel:
        cancel()
    admission = estimate_raster(page, dpi=dpi, max_raster_pixels=max_raster_pixels, clip=clip)
    kwargs = {"matrix": admission.matrix, "alpha": False}
    if admission.clip is not None:
        kwargs["clip"] = admission.clip
    if cancel:
        cancel()
    pixmap = page.get_pixmap(**kwargs)
    try:
        if cancel:
            cancel()
        _check_dimensions(pixmap.width, pixmap.height, admission.limit)
        if pixmap.width > admission.width or pixmap.height > admission.height:
            raise RasterAdmissionError("native raster exceeds admitted dimensions")
        return pixmap
    finally:
        # On exceptions do not retain native buffers in this frame's traceback.
        del pixmap


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

    def __init__(self, dpi: int = 200, *, max_raster_pixels: int | None = None) -> None:
        self.dpi = validate_raster_dpi(dpi)
        self.max_raster_pixels = effective_raster_limit(max_raster_pixels)

    def prepare(self, page: Any) -> PreparedRaster:
        pixmap = render_page_pixmap(page, dpi=self.dpi, max_raster_pixels=self.max_raster_pixels)
        try:
            raw = pixmap.tobytes("png")
        finally:
            del pixmap
        with Image.open(io.BytesIO(raw)) as loaded:
            image = ImageOps.exif_transpose(loaded).convert("RGB")
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", dpi=(self.dpi, self.dpi))
        geometry = page_geometry_from_pdf(page, pixel_width=image.width, pixel_height=image.height)
        return PreparedRaster(image=image, png_bytes=encoded.getvalue(), geometry=geometry, dpi=self.dpi)

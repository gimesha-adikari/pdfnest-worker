"""Small, bounded image preparation helpers for scanned table OCR.

The table path is deliberately conservative.  It only removes long ruling
lines when the raster contains a rectangular-looking grid with several
horizontal and vertical lines.  No image is upscaled and the returned image
has the same dimensions as the input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps


_DARK_PIXEL_THRESHOLD = 190
_MIN_LINE_COVERAGE = 0.20
_MIN_GRID_LINES = 3
_MAX_DETECTION_DIMENSION = 1600
_LINE_PADDING = 1


@dataclass(frozen=True)
class RulingLineGrid:
    """Detected long horizontal and vertical raster line bands."""

    horizontal: tuple[tuple[int, int, int, int], ...]
    vertical: tuple[tuple[int, int, int, int], ...]

    @property
    def is_table_like(self) -> bool:
        if len(self.horizontal) < _MIN_GRID_LINES or len(self.vertical) < _MIN_GRID_LINES:
            return False
        horizontal_left = min(line[2] for line in self.horizontal)
        horizontal_right = max(line[3] for line in self.horizontal)
        horizontal_top = min(line[0] for line in self.horizontal)
        horizontal_bottom = max(line[1] for line in self.horizontal)
        vertical_left = min(line[0] for line in self.vertical)
        vertical_right = max(line[1] for line in self.vertical)
        vertical_top = min(line[2] for line in self.vertical)
        vertical_bottom = max(line[3] for line in self.vertical)
        return (
            min(horizontal_right, vertical_right) > max(horizontal_left, vertical_left)
            and min(horizontal_bottom, vertical_bottom) > max(horizontal_top, vertical_top)
        )


def _sample_image(gray: Image.Image) -> tuple[Image.Image, float, float]:
    width, height = gray.size
    largest = max(width, height)
    if largest <= _MAX_DETECTION_DIMENSION:
        return gray, 1.0, 1.0
    scale = _MAX_DETECTION_DIMENSION / float(largest)
    sample_width = max(1, int(round(width * scale)))
    sample_height = max(1, int(round(height * scale)))
    sample = gray.resize((sample_width, sample_height), Image.Resampling.BILINEAR)
    return sample, width / sample_width, height / sample_height


def _bands(
    gray: Image.Image,
    *,
    horizontal: bool,
) -> tuple[tuple[int, int, int, int], ...]:
    sample, x_scale, y_scale = _sample_image(gray)
    scan = sample if horizontal else sample.transpose(Image.Transpose.TRANSPOSE)
    scan_width, scan_height = scan.size
    minimum_run = max(8, int(scan_width * _MIN_LINE_COVERAGE))
    pixels = scan.load()
    candidates: list[tuple[int, int, int]] = []
    for scan_index in range(scan_height):
        best_run = 0
        best_start = 0
        run = 0
        run_start = 0
        dark_pixels = 0
        for offset in range(scan_width):
            if pixels[offset, scan_index] <= _DARK_PIXEL_THRESHOLD:
                dark_pixels += 1
                if run == 0:
                    run_start = offset
                run += 1
                if run > best_run:
                    best_run = run
                    best_start = run_start
            else:
                run = 0
        if best_run >= minimum_run and dark_pixels >= best_run * 0.75:
            candidates.append((scan_index, best_start, best_start + best_run - 1))

    groups: list[tuple[int, int, int, int]] = []
    for scan_index, start, end in candidates:
        if groups and scan_index <= groups[-1][1] + 1:
            previous = groups[-1]
            groups[-1] = (previous[0], scan_index, min(previous[2], start), max(previous[3], end))
        else:
            groups.append((scan_index, scan_index, start, end))

    mapped: list[tuple[int, int, int, int]] = []
    for first, last, start, end in groups:
        if horizontal:
            mapped.append(
                (
                    max(0, math.floor(first * y_scale)),
                    max(0, math.ceil((last + 1) * y_scale) - 1),
                    max(0, math.floor(start * x_scale)),
                    max(0, math.ceil((end + 1) * x_scale) - 1),
                )
            )
        else:
            mapped.append(
                (
                    max(0, math.floor(first * x_scale)),
                    max(0, math.ceil((last + 1) * x_scale) - 1),
                    max(0, math.floor(start * y_scale)),
                    max(0, math.ceil((end + 1) * y_scale) - 1),
                )
            )
    return tuple(mapped)


def detect_ruling_line_grid(image: Image.Image) -> RulingLineGrid:
    """Detect long ruling-line bands without changing the supplied image."""

    gray = ImageOps.grayscale(image)
    return RulingLineGrid(_bands(gray, horizontal=True), _bands(gray, horizontal=False))


def prepare_table_ocr_image(image: Image.Image) -> tuple[Image.Image, RulingLineGrid | None]:
    """Erase a detected rectangular grid for one table-aware OCR pass.

    A new image is returned only when a grid is detected.  Otherwise the input
    object is returned unchanged so ordinary OCR keeps its existing pixels and
    segmentation behavior.
    """

    grid = detect_ruling_line_grid(image)
    if not grid.is_table_like:
        return image, None

    prepared = image.copy()
    draw = ImageDraw.Draw(prepared)
    max_x = prepared.width - 1
    max_y = prepared.height - 1
    fill = 255 if prepared.mode == "L" else (255, 255, 255)
    for y0, y1, x0, x1 in grid.horizontal:
        draw.rectangle(
            (
                max(0, x0 - _LINE_PADDING),
                max(0, y0 - _LINE_PADDING),
                min(max_x, x1 + _LINE_PADDING),
                min(max_y, y1 + _LINE_PADDING),
            ),
            fill=fill,
        )
    for x0, x1, y0, y1 in grid.vertical:
        draw.rectangle(
            (
                max(0, x0 - _LINE_PADDING),
                max(0, y0 - _LINE_PADDING),
                min(max_x, x1 + _LINE_PADDING),
                min(max_y, y1 + _LINE_PADDING),
            ),
            fill=fill,
        )
    return prepared, grid


__all__ = ["RulingLineGrid", "detect_ruling_line_grid", "prepare_table_ocr_image"]

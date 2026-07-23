import io
import json
import logging
import re
from contextlib import suppress
from statistics import median
from typing import Any, Tuple

import pymupdf as fitz
import pytesseract
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


def int_color_to_hex(color_int: int) -> str:
    try:
        return f"#{color_int & 0xFFFFFF:06x}"
    except (ValueError, TypeError):
        return "#000000"


def hex_to_rgb(hex_str: str) -> Tuple[float, float, float]:
    try:
        s = (hex_str or "#ffffff").strip().lstrip("#")
        s = s if len(s) == 6 else "ffffff"
        return int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0
    except (ValueError, TypeError):
        return (1.0, 1.0, 1.0)


def pixmap_to_image(pix: fitz.Pixmap) -> Image.Image:
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return img if img.mode == "RGB" else img.convert("RGB")


def sample_text_color_hex(image: Image.Image, rect: fitz.Rect, zoom: float = 2.0) -> str:
    """
    Samples the average color of the dark pixels (text) in a bounding box.
    """
    try:
        crop_box = (
            int(rect.x0 * zoom),
            int(rect.y0 * zoom),
            int(rect.x1 * zoom),
            int(rect.y1 * zoom),
        )
        cropped = image.crop(crop_box).convert("RGB")
        if cropped.width <= 0 or cropped.height <= 0:
            return "#000000"

        pixels = list(cropped.getdata())
        dark_pixels = [p for p in pixels if sum(p) < 380]

        if not dark_pixels:
            return "#000000"

        r = sum(p[0] for p in dark_pixels) // len(dark_pixels)
        g = sum(p[1] for p in dark_pixels) // len(dark_pixels)
        b = sum(p[2] for p in dark_pixels) // len(dark_pixels)

        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#000000"


def analyze_font_attributes(image: Image.Image, rect: fitz.Rect, zoom: float = 2.0) -> Tuple[str, bool]:
    """
    Dynamically analyzes a cropped image region to determine font attributes.
    """
    try:
        crop_box = (
            int(rect.x0 * zoom),
            int(rect.y0 * zoom),
            int(rect.x1 * zoom),
            int(rect.y1 * zoom),
        )
        cropped = image.crop(crop_box).convert("L")

        if cropped.width <= 0 or cropped.height <= 0:
            return "tiro", False

        pixels = list(cropped.getdata())
        dark_pixels = sum(1 for p in pixels if p < 120)
        total_pixels = len(pixels)
        density = dark_pixels / float(total_pixels) if total_pixels > 0 else 0

        is_bold = density > 0.28
        font_style = "tiro"

        return font_style, is_bold
    except Exception:
        return "tiro", False


def sample_background_hex(page: fitz.Page, rect: fitz.Rect) -> str:
    samples: list[tuple[int, int, int]] = []
    pad = 3.0
    candidate_rects = [
        fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x0 - 1, rect.y0 - 1),
        fitz.Rect(rect.x1 + 1, rect.y0 - pad, rect.x1 + pad, rect.y0 - 1),
        fitz.Rect(rect.x0 - pad, rect.y1 + 1, rect.x0 - 1, rect.y1 + pad),
        fitz.Rect(rect.x1 + 1, rect.y1 + 1, rect.x1 + pad, rect.y1 + pad),
    ]

    for clip in candidate_rects:
        clip = clip & page.rect
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            continue

        try:
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(1, 1), alpha=False)
            if pix.samples and len(pix.samples) >= 3:
                r, g, b = pix.samples[0], pix.samples[1], pix.samples[2]
                if r > 220 and g > 220 and b > 220:
                    samples.append((r, g, b))
        except fitz.FitzError:
            continue

    if not samples:
        return "#ffffff"

    r = sum(c[0] for c in samples) // len(samples)
    g = sum(c[1] for c in samples) // len(samples)
    b = sum(c[2] for c in samples) // len(samples)

    if r < 235 or g < 235 or b < 235:
        return "#ffffff"

    return f"#{r:02x}{g:02x}{b:02x}"


def group_words_by_line(word_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not word_items:
        return []

    heights = [max(1.0, float(w["rect"].height)) for w in word_items]
    med_height = median(heights)
    y_tol = max(3.0, med_height * 0.45)
    max_x_gap = 10.0

    lines: list[dict[str, Any]] = []

    sorted_words = sorted(
        word_items,
        key=lambda w: ((w["rect"].y0 + w["rect"].y1) / 2.0, w["rect"].x0)
    )

    for item in sorted_words:
        rect = item["rect"]
        item_y_center = (rect.y0 + rect.y1) / 2.0
        placed = False

        for line in lines:
            line_y_center = (line["y0"] + line["y1"]) / 2.0
            if abs(item_y_center - line_y_center) <= y_tol:
                gap = rect.x0 - line["x1"]
                if 0 <= gap <= max_x_gap:
                    line["items"].append(item)
                    line["x0"] = min(line["x0"], rect.x0)
                    line["x1"] = max(line["x1"], rect.x1)
                    line["y0"] = min(line["y0"], rect.y0)
                    line["y1"] = max(line["y1"], rect.y1)
                    placed = True
                    break

        if not placed:
            lines.append({
                "items": [item],
                "x0": rect.x0,
                "x1": rect.x1,
                "y0": rect.y0,
                "y1": rect.y1,
            })

    return lines


def is_valid_ocr_word(text: str, conf: float) -> bool:
    if conf < 30.0:
        return False
    if len(text) == 1 and not text.isalnum():
        return False
    if re.match(r"^[\-_\|\\/\.\,\;\:\'\"]+$", text):
        return False
    return True


def ocr_words_for_page(page: fitz.Page, zoom: float = 2.0, lang: str = "eng") -> Tuple[list[dict[str, Any]], Image.Image]:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = pixmap_to_image(pix)

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, lang=lang)
    items: list[dict[str, Any]] = []

    for i, text in enumerate(data.get("text", [])):
        text = str(text).strip()
        if not text:
            continue

        try:
            conf = float(data.get("conf", ["-1"])[i])
        except (ValueError, TypeError, IndexError):
            conf = -1.0

        if not is_valid_ocr_word(text, conf):
            continue

        left = float(data["left"][i]) / zoom
        top = float(data["top"][i]) / zoom
        width = float(data["width"][i]) / zoom
        height = float(data["height"][i]) / zoom
        rect = fitz.Rect(left, top, left + width, top + height)
        items.append({"rect": rect, "text": text, "conf": conf})

    return items, image


def deduplicate_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for el in elements:
        r1 = fitz.Rect(el["x"], el["y"], el["x"] + el["width"], el["y"] + el["height"])
        is_dup = False
        for u in unique:
            r2 = fitz.Rect(u["x"], u["y"], u["x"] + u["width"], u["y"] + u["height"])
            if el["text"] == u["text"] and (r1.intersects(r2) or abs(r1.y0 - r2.y0) < 1.2):
                is_dup = True
                break
        if not is_dup:
            unique.append(el)
    return unique


def extract_native_page(page: fitz.Page, page_number: int) -> dict[str, Any]:
    text_dict = page.get_text("dict") or {}
    blocks = text_dict.get("blocks", []) or []
    native_words = page.get_text("words") or []

    elements: list[dict[str, Any]] = []
    text_block_count = image_block_count = 0

    for block in blocks:
        if not isinstance(block, dict):
            continue

        if block.get("type") == 1:
            image_block_count += 1
            continue

        bbox = block.get("bbox")
        if block.get("type") != 0 or not bbox or len(bbox) < 4:
            continue

        for line in block.get("lines", []):
            line_bbox = line.get("bbox")
            if not line_bbox or len(line_bbox) < 4:
                continue

            rect = fitz.Rect(*line_bbox)
            if rect.height <= 0 or rect.width <= 0:
                continue

            line_text_parts = []
            font_size = 9.5
            font_name = "sans-serif"
            text_color_hex = "#000000"

            spans = line.get("spans", []) or []
            for span in spans:
                if span_text := span.get("text", ""):
                    line_text_parts.append(span_text)
                font_size = span.get("size", font_size)
                font_name = span.get("font", font_name)
                if "color" in span:
                    text_color_hex = int_color_to_hex(int(span["color"]))

            line_text = "".join(line_text_parts).strip()
            if not line_text:
                continue

            elements.append({
                "text": line_text,
                "original_text": line_text,
                "x": rect.x0,
                "y": rect.y0,
                "width": rect.width,
                "height": rect.height,
                "size": round(font_size, 1),
                "font": font_name,
                "bg_color": "transparent",
                "text_color": text_color_hex,
                "transparent_bg": True,
            })
            text_block_count += 1

    elements = deduplicate_elements(elements)
    word_count = len(native_words)
    kind = "mixed"
    if word_count == 0 and image_block_count == 0:
        kind = "blank"
    elif word_count == 0 and image_block_count > 0:
        kind = "scanned"
    elif word_count > 0 and image_block_count == 0:
        kind = "text"

    return {
        "page_num": page_number,
        "width": page.rect.width,
        "height": page.rect.height,
        "elements": elements,
        "kind": kind,
        "has_selectable_text": word_count > 0,
        "word_count": word_count,
        "text_block_count": text_block_count,
        "image_block_count": image_block_count,
    }


def extract_ocr_page(page: fitz.Page, page_number: int) -> dict[str, Any]:
    word_items, page_image = ocr_words_for_page(page)
    lines = group_words_by_line(word_items)
    elements: list[dict[str, Any]] = []

    for line in lines:
        items = sorted(line["items"], key=lambda w: w["rect"].x0)
        text = " ".join(str(w["text"]) for w in items).strip()
        if not text:
            continue

        rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
        if rect.is_empty or rect.width < 3.0:
            continue

        word_heights = [w["rect"].height for w in items if w["rect"].height > 0]
        # Use the maximum height (tallest letter) of the line to establish scale
        max_height = max(word_heights) if word_heights else rect.height

        # Bounding boxes map exactly to visual ink (Cap Height or Ascender Height).
        # A true typographic point size (Em-Square) is always physically larger than the ink box.
        # Multiplying by ~1.15 correctly scales an OCR height to standard font point size.
        dynamic_font_size = max_height * 1.15

        font_family, is_bold = analyze_font_attributes(page_image, rect)
        sampled_color = sample_text_color_hex(page_image, rect)

        if font_family == "tiro":
            font_code = "tibo" if is_bold else "tiro"
        else:
            font_code = "hebo" if is_bold else "helv"

        elements.append({
            "text": text,
            "original_text": text,
            "x": rect.x0,
            "y": rect.y0,
            "width": rect.width,
            "height": rect.height,
            "size": round(dynamic_font_size, 1),
            "font": font_code,
            "bg_color": "transparent",
            "text_color": sampled_color,
            "transparent_bg": True,
        })

    elements = deduplicate_elements(elements)

    return {
        "page_num": page_number,
        "width": page.rect.width,
        "height": page.rect.height,
        "elements": elements,
        "kind": "scanned" if elements else "blank",
        "is_ocr": True,
        "has_selectable_text": False,
        "word_count": 0,
        "text_block_count": 0,
        "image_block_count": 0,
    }


def extract_document(input_path: str, password: str | None = None) -> dict[str, Any]:
    with fitz.open(input_path) as doc:
        if doc.needs_pass:
            if not password:
                raise RuntimeError("PDF is password protected but no password was provided")
            if doc.authenticate(password) <= 0:
                raise RuntimeError("Invalid PDF password")

        pages: list[dict[str, Any]] = []
        for i in range(doc.page_count):
            page = doc[i]
            if len(page.get_text("words") or []) > 0:
                pages.append(extract_native_page(page, i + 1))
            else:
                pages.append(extract_ocr_page(page, i + 1))

        return {"success": True, "pages": pages}


def compile_document(original_pdf_path: str, output_pdf_path: str, pages_json_path: str) -> None:
    with open(pages_json_path, "r", encoding="utf-8") as f:
        layout_data = json.load(f)

    pages = layout_data.get("pages", [])

    with fitz.open(original_pdf_path) as doc:
        for page_idx, page_data in enumerate(pages):
            if page_idx >= len(doc):
                continue

            page = doc[page_idx]
            elements = page_data.get("elements", []) or []
            is_ocr_page = page_data.get("is_ocr", False) or page_data.get("kind") == "scanned"

            if is_ocr_page:
                zoom = 2.0
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                img = pixmap_to_image(pix)
                draw = ImageDraw.Draw(img)

                for element in elements:
                    try:
                        text_val = str(element.get("text", "")).strip()
                        orig_text = str(element.get("original_text", "")).strip()

                        if text_val == orig_text:
                            continue

                        w = float(element.get("width", 0))
                        h = float(element.get("height", 0))
                        if w <= 0 or h <= 0:
                            continue

                        x0 = float(element.get("x", 0)) * zoom
                        y0 = float(element.get("y", 0)) * zoom
                        w_scaled = w * zoom
                        h_scaled = h * zoom
                        bg_hex = element.get("bg_color", "#ffffff")
                        bg_hex = "#ffffff" if bg_hex == "transparent" else bg_hex

                        draw.rectangle(
                            [x0 - 1, y0 - 1, x0 + w_scaled + 1, y0 + h_scaled + 1],
                            fill=bg_hex
                        )
                    except (ValueError, TypeError):
                        continue

                img_bytes = io.BytesIO()
                img.save(img_bytes, format="JPEG", quality=95)

                for item in page.get_images():
                    with suppress(Exception):
                        doc.delete_xref(item[0])

                page.clean_contents()
                page.insert_image(page.rect, stream=img_bytes.getvalue())

            else:
                for element in elements:
                    try:
                        w = float(element.get("width", 0))
                        h = float(element.get("height", 0))
                        if w <= 0 or h <= 0:
                            continue

                        x0 = float(element.get("x", 0))
                        y0 = float(element.get("y", 0))
                        bg_hex = element.get("bg_color", "#ffffff")
                        bg_hex = "#ffffff" if bg_hex == "transparent" else bg_hex
                        color_rgb = hex_to_rgb(bg_hex)

                        rect = fitz.Rect(x0 - 1, y0 - 1, x0 + w + 1, y0 + h + 1)
                        page.add_redact_annot(rect, fill=color_rgb)
                    except (ValueError, TypeError):
                        continue

                page.apply_redactions()

            for element in elements:
                text_val = str(element.get("text", "")).strip()
                orig_text = str(element.get("original_text", "")).strip()

                if is_ocr_page and text_val == orig_text:
                    continue

                if not text_val:
                    continue

                try:
                    x0 = float(element.get("x", 0))
                    y0 = float(element.get("y", 0))
                    w = float(element.get("width", 0))
                    h = float(element.get("height", 0))

                    fallback_size = h * 1.15
                    font_size = float(element.get("size", fallback_size))

                    font_code = str(element.get("font", "tiro")).lower()
                    valid_pdf_fonts = ["helv", "hebo", "tiro", "tibo", "cour", "cobo", "symb", "zadb"]
                    if font_code not in valid_pdf_fonts:
                        font_code = "tiro"

                    text_color_hex = element.get("text_color", "#000000")
                    color_rgb = hex_to_rgb(text_color_hex)

                    # Create an intentionally expanded rect for rendering.
                    # If the rect is too tight, PyMuPDF will shrink the text to force fit it.
                    expanded_render_rect = fitz.Rect(x0, y0 - (h * 0.1), x0 + w + 20, y0 + h * 1.5)

                    rc = page.insert_textbox(
                        expanded_render_rect,
                        text_val,
                        fontsize=font_size,
                        fontname=font_code,
                        color=color_rgb,
                        align=0,
                    )

                    if rc < 0:
                        page.insert_text(
                            fitz.Point(x0, y0 + h * 0.85),
                            text_val,
                            fontsize=font_size,
                            fontname=font_code,
                            color=color_rgb,
                        )
                except (ValueError, TypeError, fitz.FitzError) as e:
                    logger.warning("Failed to insert text on page %s: %s", page_idx, e)
                    continue

        doc.save(output_pdf_path, garbage=3, deflate=True)
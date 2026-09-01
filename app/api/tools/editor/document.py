import difflib
import io
import json
import logging
import re
from contextlib import suppress
from statistics import median
from typing import Any, Callable, Tuple

import pymupdf as fitz
import pytesseract
from PIL import Image, ImageDraw

from app.core.editor_ocr_projection import (
    first_failed_editor_page,
    project_editor_result,
)
from app.core.ocr_v2.errors import EngineUnavailableError
from app.core.ocr_v2.orchestration import OCRV2Worker
from app.core.ocr_v2.routing import RoutePolicy
from app.core.ocr_v2.validation import OCRProfile

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
    """Estimate text color from dark pixels in an extracted bounding box."""
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
    """Estimate the fallback font style from the cropped image region."""
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
    del pix

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
    for index, element in enumerate(elements, start=1):
        element["id"] = f"p{page_number}-text-{index}"
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
        max_height = max(word_heights) if word_heights else rect.height

        # OCR boxes measure ink height, while PDF font sizes use the larger em square.
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
    for index, element in enumerate(elements, start=1):
        element["id"] = f"p{page_number}-text-{index}"
    page_image.close()
    del page_image

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


def extract_document(
    input_path: str,
    password: str | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    with fitz.open(input_path) as doc:
        if doc.needs_pass:
            if not password:
                raise RuntimeError("PDF is password protected but no password was provided")
            if doc.authenticate(password) <= 0:
                raise RuntimeError("Invalid PDF password")

        pages: list[dict[str, Any]] = []
        for i in range(doc.page_count):
            if cancellation_check is not None:
                cancellation_check()
            page = doc[i]
            if len(page.get_text("words") or []) > 0:
                pages.append(extract_native_page(page, i + 1))
            else:
                pages.append(extract_ocr_page(page, i + 1))
        return {"success": True, "pages": pages}


def extract_document_v2(
    input_path: str,
    password: str | None = None,
    cancellation_check: Callable[[], None] | None = None,
    page_progress_callback: Callable[[int, int, Any], None] | None = None,
) -> dict[str, Any]:
    """Extract editor layout through the shared OCR V2 canonical result.

    This is an explicit V2 editor mode.  The legacy ``extract_document``
    function remains available to V1 routes and retains its historical direct
    OCR behavior until parity/migration is separately approved.
    """
    worker = OCRV2Worker(
        route_policy=RoutePolicy(preferred_engine="tesseract_v2", fallback_engine="tesseract_v2"),
        max_raster_pixels=25_000_000,
    )
    result = worker.process_document(
        input_path,
        password=password,
        language="eng",
        profile=OCRProfile.OCR_TEXT_V2,
        cancellation_check=cancellation_check,
        page_progress_callback=page_progress_callback,
    )
    failed = first_failed_editor_page(result)
    if failed is not None:
        if failed.failure_code == "EngineUnavailableError":
            raise EngineUnavailableError("OCR V2 editor extraction engine is unavailable")
        raise RuntimeError("OCR V2 editor extraction failed for a page")

    return project_editor_result(result)


def is_element_dirty(element: dict[str, Any]) -> bool:
    """Determine whether a layout element has modified text content or style overrides."""
    if not isinstance(element, dict):
        return False

    raw_text = element.get("text")
    raw_orig = element.get("original_text")

    text_val = "" if raw_text is None else str(raw_text)
    orig_val = "" if raw_orig is None else str(raw_orig)

    if text_val != orig_val:
        return True

    # Check style object overrides
    style = element.get("style")
    if isinstance(style, dict) and style:
        for key in ["fontFamily", "fontSize", "color", "bold", "italic", "underline", "strikethrough", "background"]:
            if style.get(key) is not None:
                return True

    # Check root-level formatting overrides
    for key in ["bold", "italic", "underline", "strikethrough"]:
        if element.get(key) is True:
            return True

    bg_color = element.get("bg_color")
    if bg_color and bg_color not in ("transparent", "none", "#ffffff"):
        return True

    return False


def tokenize_words(text: str) -> list[dict[str, Any]]:
    """Extract word tokens from text with start and end character offsets."""
    words: list[dict[str, Any]] = []
    if not text:
        return words

    for m in re.finditer(r"\S+", text):
        words.append({
            "word": m.group(0),
            "start": m.start(),
            "end": m.end(),
        })
    return words


def compute_text_diff(original_text: str, edited_text: str) -> list[dict[str, Any]]:
    """Compute semantic edit diff operations between original_text and edited_text.

    Uses Word-First, Character-Second diffing to ensure whole words are targeted for word edits,
    while preserving exact character offsets and whitespace without trimming.
    """
    orig = "" if original_text is None else str(original_text)
    edit = "" if edited_text is None else str(edited_text)

    orig_tokens = tokenize_words(orig)
    edit_tokens = tokenize_words(edit)

    orig_word_list = [t["word"] for t in orig_tokens]
    edit_word_list = [t["word"] for t in edit_tokens]

    diff_ops: list[dict[str, Any]] = []

    # 1. Fast-path: Equal number of word tokens (1-to-1 word alignment)
    if len(orig_word_list) == len(edit_word_list) and len(orig_word_list) > 0:
        i = 0
        while i < len(orig_word_list):
            if orig_word_list[i] != edit_word_list[i]:
                start_i = i
                while i < len(orig_word_list) and orig_word_list[i] != edit_word_list[i]:
                    i += 1
                end_i = i

                o_start = orig_tokens[start_i]["start"]
                o_end = orig_tokens[end_i - 1]["end"]
                e_start = edit_tokens[start_i]["start"]
                e_end = edit_tokens[end_i - 1]["end"]

                o_sub = orig[o_start:o_end]
                e_sub = edit[e_start:e_end]

                # Check for sub-word punctuation edit within a single word token
                if (end_i - start_i == 1) and len(orig_word_list[start_i]) > 1:
                    matcher = difflib.SequenceMatcher(None, orig_word_list[start_i], edit_word_list[start_i])
                    sub_ops = matcher.get_opcodes()
                    # If diff is single punctuation replacement at end/start of word (e.g. 'Engineer.' -> 'Engineer,')
                    if len(sub_ops) == 2 and sub_ops[0][0] == "equal" and sub_ops[1][0] == "replace":
                        sub_tag, s_i1, s_i2, s_j1, s_j2 = sub_ops[1]
                        sub_orig_char = orig_word_list[start_i][s_i1:s_i2]
                        if not sub_orig_char.isalnum():
                            diff_ops.append({
                                "operation": "replace",
                                "original_start": o_start + s_i1,
                                "original_end": o_start + s_i2,
                                "original_substring": sub_orig_char,
                                "edited_start": e_start + s_j1,
                                "edited_end": e_start + s_j2,
                                "replacement_substring": edit_word_list[start_i][s_j1:s_j2],
                                "sub_word_punctuation": True,
                            })
                            i = end_i
                            continue

                diff_ops.append({
                    "operation": "replace",
                    "original_start": o_start,
                    "original_end": o_end,
                    "original_substring": o_sub,
                    "edited_start": e_start,
                    "edited_end": e_end,
                    "replacement_substring": e_sub,
                    "sub_word_punctuation": False,
                })
            else:
                i += 1
        return diff_ops

    # 2. General-path for unequal token counts using SequenceMatcher on word tokens
    matcher = difflib.SequenceMatcher(None, orig_word_list, edit_word_list)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if i1 < len(orig_tokens):
            o_start = orig_tokens[i1]["start"]
        elif orig_tokens:
            o_start = orig_tokens[-1]["end"]
        else:
            o_start = 0

        if i2 > 0 and i2 - 1 < len(orig_tokens):
            o_end = orig_tokens[i2 - 1]["end"]
        else:
            o_end = o_start

        if j1 < len(edit_tokens):
            e_start = edit_tokens[j1]["start"]
        elif edit_tokens:
            e_start = edit_tokens[-1]["end"]
        else:
            e_start = 0

        if j2 > 0 and j2 - 1 < len(edit_tokens):
            e_end = edit_tokens[j2 - 1]["end"]
        else:
            e_end = e_start

        o_sub = orig[o_start:o_end] if i1 < i2 else ""
        e_sub = edit[e_start:e_end] if j1 < j2 else ""

        diff_ops.append({
            "operation": tag,
            "original_start": o_start,
            "original_end": o_end,
            "original_substring": o_sub,
            "edited_start": e_start,
            "edited_end": e_end,
            "replacement_substring": e_sub,
            "sub_word_punctuation": False,
        })

    return diff_ops


def extract_page_char_map(page: fitz.Page) -> list[dict[str, Any]]:
    """Extract line, span, and character geometry map from PyMuPDF page.get_text('rawdict')."""
    try:
        raw = page.get_text("rawdict") or {}
    except Exception:
        return []

    blocks = raw.get("blocks", []) or []
    lines_map: list[dict[str, Any]] = []

    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            line_bbox = line.get("bbox")
            if not line_bbox or len(line_bbox) < 4:
                continue

            chars: list[dict[str, Any]] = []
            spans = line.get("spans", []) or []

            for span in spans:
                font_name = span.get("font", "sans-serif")
                font_size = span.get("size", 10.0)
                font_color = span.get("color", 0)
                color_hex = int_color_to_hex(font_color) if isinstance(font_color, int) else "#000000"

                for char_info in span.get("chars", []) or []:
                    c = char_info.get("c", "")
                    bbox = char_info.get("bbox")
                    origin = char_info.get("origin")
                    if not bbox or len(bbox) < 4:
                        continue

                    chars.append({
                        "c": c,
                        "bbox": bbox,
                        "origin": origin if origin and len(origin) >= 2 else (bbox[0], bbox[3]),
                        "font": font_name,
                        "size": font_size,
                        "color": color_hex,
                    })

            line_text = "".join(c["c"] for c in chars)
            if not line_text.strip() and not chars:
                continue

            lines_map.append({
                "bbox": line_bbox,
                "text": line_text,
                "chars": chars,
                "y0": float(line_bbox[1]),
                "y1": float(line_bbox[3]),
                "x0": float(line_bbox[0]),
                "x1": float(line_bbox[2]),
            })

    return lines_map


def resolve_surgical_targets(page: fitz.Page, element: dict[str, Any]) -> list[dict[str, Any]]:
    """Determine fine-grained surgical targets (bboxes and metadata) for modified text in a dirty element."""
    if not is_element_dirty(element):
        return []

    orig_text = str(element.get("original_text", ""))
    edit_text = str(element.get("text", ""))

    diff_ops = compute_text_diff(orig_text, edit_text)

    # Style-only edit support: target specific word/substring if specified in payload
    if not diff_ops and is_element_dirty(element):
        target_sub = element.get("target_substring") or element.get("original_substring") or element.get("target_text")
        if target_sub and str(target_sub) in orig_text:
            target_sub_str = str(target_sub)
            sel_start = element.get("selection_start")
            sel_end = element.get("selection_end")
            if sel_start is not None and sel_end is not None and 0 <= int(sel_start) <= len(orig_text) and int(sel_end) <= len(orig_text):
                s_idx = int(sel_start)
                e_idx = int(sel_end)
            else:
                s_idx = orig_text.find(target_sub_str)
                e_idx = s_idx + len(target_sub_str)

            diff_ops = [{
                "operation": "replace",
                "original_start": s_idx,
                "original_end": e_idx,
                "original_substring": target_sub_str,
                "edited_start": s_idx,
                "edited_end": e_idx,
                "replacement_substring": target_sub_str,
                "sub_word_punctuation": False,
            }]
        elif target_sub:
            # Target specified but not found in original text -> safety rule: return empty list, do NOT fall back to line!
            return []
        else:
            # Target full original_text when explicit style override applies to full element
            diff_ops = [{
                "operation": "replace",
                "original_start": 0,
                "original_end": len(orig_text),
                "original_substring": orig_text,
                "edited_start": 0,
                "edited_end": len(edit_text),
                "replacement_substring": edit_text,
                "sub_word_punctuation": False,
            }]

    if not diff_ops:
        return []

    elem_x = float(element.get("x", 0))
    elem_y = float(element.get("y", 0))
    elem_w = float(element.get("width", 0))
    elem_h = float(element.get("height", 0))
    elem_size = float(element.get("size", 10.0))
    elem_font = str(element.get("font", "sans-serif"))
    elem_color = str(element.get("text_color", "#000000"))

    line_map = extract_page_char_map(page)

    matched_line = None
    y_matches = [
        line for line in line_map
        if abs(line["y0"] - elem_y) <= 4.0 or abs((line["y0"] + line["y1"]) / 2.0 - (elem_y + elem_h / 2.0)) <= 4.0
    ]

    if len(y_matches) == 1:
        matched_line = y_matches[0]
    elif len(y_matches) > 1:
        matched_line = max(
            y_matches,
            key=lambda l: difflib.SequenceMatcher(None, l["text"], orig_text).ratio()
        )
    else:
        text_matches = [
            line for line in line_map
            if orig_text and (orig_text in line["text"] or line["text"] in orig_text or difflib.SequenceMatcher(None, line["text"], orig_text).ratio() > 0.8)
        ]
        if text_matches:
            matched_line = max(
                text_matches,
                key=lambda l: difflib.SequenceMatcher(None, l["text"], orig_text).ratio()
            )

    targets: list[dict[str, Any]] = []

    for op in diff_ops:
        tag = op["operation"]
        i1 = op["original_start"]
        i2 = op["original_end"]
        orig_sub = op["original_substring"]
        repl_sub = op["replacement_substring"]

        next_word_x = None

        if matched_line and matched_line["chars"]:
            line_chars = matched_line["chars"]

            # Geometrical neighbor calculation: find x0 of next non-space character after i2
            if i2 < len(line_chars):
                for char_info in line_chars[i2:]:
                    c_str = char_info.get("c", "")
                    if c_str and not c_str.isspace():
                        next_word_x = float(char_info["bbox"][0])
                        break

            if tag in ("replace", "delete"):
                if 0 <= i1 < len(line_chars) and 0 < i2 <= len(line_chars) and i1 < i2:
                    target_chars = line_chars[i1:i2]
                    min_x = min(c["bbox"][0] for c in target_chars)
                    min_y = min(c["bbox"][1] for c in target_chars)
                    max_x = max(c["bbox"][2] for c in target_chars)
                    max_y = max(c["bbox"][3] for c in target_chars)
                    baseline_y = float(sum(c["origin"][1] for c in target_chars) / len(target_chars))

                    first_char = target_chars[0]
                    font_info = {
                        "name": first_char.get("font", elem_font),
                        "size": first_char.get("size", elem_size),
                        "color": first_char.get("color", elem_color),
                    }

                    granularity = "character" if op.get("sub_word_punctuation") else "word"

                    targets.append({
                        "operation": tag,
                        "original_substring": orig_sub,
                        "replacement_substring": repl_sub,
                        "original_start": i1,
                        "original_end": i2,
                        "granularity": granularity,
                        "target_bbox": [min_x, min_y, max_x, max_y],
                        "baseline_y": baseline_y,
                        "font_info": font_info,
                        "insertion_point": None,
                        "character_count": len(target_chars),
                        "next_word_x": next_word_x,
                        "confidence": "exact",
                    })
                    continue

            elif tag == "insert":
                if i1 > 0 and i1 - 1 < len(line_chars):
                    anchor_char = line_chars[i1 - 1]
                    ins_x = float(anchor_char["bbox"][2])
                    ins_y = float(anchor_char["origin"][1])
                    font_info = {
                        "name": anchor_char.get("font", elem_font),
                        "size": anchor_char.get("size", elem_size),
                        "color": anchor_char.get("color", elem_color),
                    }
                elif i1 == 0 and len(line_chars) > 0:
                    anchor_char = line_chars[0]
                    ins_x = float(anchor_char["bbox"][0])
                    ins_y = float(anchor_char["origin"][1])
                    font_info = {
                        "name": anchor_char.get("font", elem_font),
                        "size": anchor_char.get("size", elem_size),
                        "color": anchor_char.get("color", elem_color),
                    }
                else:
                    ins_x = elem_x
                    ins_y = elem_y + elem_h * 0.85
                    font_info = {"name": elem_font, "size": elem_size, "color": elem_color}

                targets.append({
                    "operation": tag,
                    "original_substring": orig_sub,
                    "replacement_substring": repl_sub,
                    "original_start": i1,
                    "original_end": i2,
                    "granularity": "insertion_point",
                    "target_bbox": None,
                    "baseline_y": ins_y,
                    "font_info": font_info,
                    "insertion_point": [ins_x, ins_y],
                    "character_count": 0,
                    "next_word_x": next_word_x,
                    "confidence": "exact",
                })
                continue

        # Fallback to line element bounding box if character mapping failed
        fallback_bbox = [elem_x, elem_y, elem_x + elem_w, elem_y + elem_h]
        targets.append({
            "operation": tag,
            "original_substring": orig_sub,
            "replacement_substring": repl_sub,
            "original_start": i1,
            "original_end": i2,
            "granularity": "line",
            "target_bbox": None if tag == "insert" else fallback_bbox,
            "baseline_y": elem_y + elem_h * 0.85,
            "font_info": {"name": elem_font, "size": elem_size, "color": elem_color},
            "insertion_point": [elem_x, elem_y + elem_h * 0.85] if tag == "insert" else None,
            "character_count": len(orig_sub),
            "next_word_x": None,
            "confidence": "line_fallback",
        })

    return targets


def compile_document(
    original_pdf_path: str,
    output_pdf_path: str,
    pages_json_path: str,
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    with open(pages_json_path, "r", encoding="utf-8") as f:
        layout_data = json.load(f)

    pages = layout_data.get("pages", [])

    with fitz.open(original_pdf_path) as doc:
        for page_idx, page_data in enumerate(pages):
            if cancellation_check is not None:
                cancellation_check()
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
                    if not is_element_dirty(element):
                        continue

                    try:
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

                # Render replacement text and formatting overlays for dirty elements on OCR/scanned page
                for element in elements:
                    if not is_element_dirty(element):
                        continue

                    repl_text = element.get("text", "")
                    if not repl_text:
                        continue

                    elem_x = float(element.get("x", 0))
                    elem_y = float(element.get("y", 0))
                    elem_w = float(element.get("width", 0))
                    elem_h = float(element.get("height", 0))

                    target = {
                        "operation": "replace",
                        "original_substring": element.get("original_text", ""),
                        "replacement_substring": repl_text,
                        "target_bbox": [elem_x, elem_y, elem_x + elem_w, elem_y + elem_h],
                        "baseline_y": elem_y + elem_h * 0.85,
                        "font_info": {
                            "name": element.get("font", "helv"),
                            "size": element.get("size", 10.0),
                            "color": element.get("text_color", "#000000"),
                        },
                    }

                    render_surgical_replacement(page, target, element=element, skip_redaction=True)

            else:
                # Native PDF page: apply surgical replacement & style engine
                page_targets = []
                for element in elements:
                    if not is_element_dirty(element):
                        continue

                    targets = resolve_surgical_targets(page, element)
                    if not targets:
                        targets = [{
                            "operation": "replace",
                            "original_substring": element.get("original_text", ""),
                            "replacement_substring": element.get("text", ""),
                            "target_bbox": [
                                float(element.get("x", 0)),
                                float(element.get("y", 0)),
                                float(element.get("x", 0)) + float(element.get("width", 0)),
                                float(element.get("y", 0)) + float(element.get("height", 0)),
                            ],
                            "baseline_y": float(element.get("y", 0)) + float(element.get("height", 0)) * 0.85,
                            "font_info": {
                                "name": element.get("font", "helv"),
                                "size": element.get("size", 10.0),
                                "color": element.get("text_color", "#000000"),
                            },
                        }]

                    for target in targets:
                        page_targets.append((target, element))

                # Step 1: Add all redaction annotations on page
                has_redactions = False
                for target, element in page_targets:
                    op = target.get("operation", "replace")
                    bbox = target.get("target_bbox")
                    if op in ("replace", "delete") and bbox and len(bbox) >= 4:
                        page.add_redact_annot(fitz.Rect(*bbox), fill=None)
                        has_redactions = True

                # Step 2: Apply all redactions in a single clean pass
                if has_redactions:
                    page.apply_redactions()

                # Step 3: Render replacement text, user highlights, and decorations
                for target, element in page_targets:
                    render_surgical_replacement(page, target, element=element, fill_color=None, skip_redaction=True)

        doc.save(output_pdf_path, garbage=3, deflate=True)


def resolve_pdf_font_variant(family: str, bold: bool = False, italic: bool = False) -> str:
    """Map font family and bold/italic flags to PyMuPDF font code."""
    f = str(family or "").lower()

    if "times" in f or "tiro" in f or "serif" in f:
        if bold and italic:
            return "tibi"
        if bold:
            return "tibo"
        if italic:
            return "tiit"
        return "tiro"
    elif "cour" in f or "mono" in f:
        if bold and italic:
            return "cobi"
        if bold:
            return "cobo"
        if italic:
            return "coit"
        return "cour"
    else:
        if bold and italic:
            return "hebi"
        if bold:
            return "hebo"
        if italic:
            return "heit"
        return "helv"


def compute_effective_style(element: dict[str, Any], font_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute effective style by merging original PDF font info with user style overrides.

    original_style + user_style_overrides = final_style
    """
    font_info = font_info or {}
    element = element or {}
    style_override = element.get("style") or {}
    if not isinstance(style_override, dict):
        style_override = {}

    orig_font = str(font_info.get("name", element.get("font", "helv"))).lower()
    orig_size = float(font_info.get("size", element.get("size", 10.0)))
    orig_color = str(font_info.get("color", element.get("text_color", "#000000")))

    font_family_override = style_override.get("fontFamily")
    if not font_family_override or font_family_override == "original":
        font_family = orig_font
        font_source = "original_embedded" if font_info.get("embedded") else "original_pdf"
    else:
        font_family = str(font_family_override).lower()
        font_source = "user_override"

    font_size = float(style_override.get("fontSize") or element.get("size", orig_size))
    color = str(style_override.get("color") or element.get("text_color", orig_color))

    bold = bool(style_override.get("bold", element.get("bold", False)))
    italic = bool(style_override.get("italic", element.get("italic", False)))

    font_code = resolve_pdf_font_variant(font_family, bold=bold, italic=italic)

    underline = bool(style_override.get("underline", element.get("underline", False)))
    strikethrough = bool(style_override.get("strikethrough", element.get("strikethrough", False)))

    user_bg = style_override.get("background") or element.get("bg_color")
    bg_enabled = False
    bg_color = None

    if isinstance(user_bg, dict):
        bg_enabled = bool(user_bg.get("enabled", True))
        bg_color = user_bg.get("color")
    elif isinstance(user_bg, str) and user_bg and user_bg not in ("transparent", "none"):
        bg_enabled = True
        bg_color = user_bg

    return {
        "font_code": font_code,
        "font_family": font_family,
        "font_source": font_source,
        "orig_font": orig_font,
        "font_size": font_size,
        "color": color,
        "bold": bold,
        "italic": italic,
        "underline": underline,
        "strikethrough": strikethrough,
        "user_bg_enabled": bg_enabled,
        "user_bg_color": bg_color,
    }


def render_surgical_replacement(
    page: fitz.Page,
    target: dict[str, Any],
    element: dict[str, Any] | None = None,
    fill_color: tuple[float, float, float] | None = None,
    font_file: str | None = None,
    skip_redaction: bool = False,
) -> dict[str, Any]:
    """Render surgical word/substring replacement with rich formatting and background preservation.

    Returns detailed metrics on target replacement.
    """
    element = element or {}
    op = target.get("operation", "replace")
    target_bbox = target.get("target_bbox")
    baseline_y = float(target.get("baseline_y", 0.0))
    font_info = target.get("font_info", {}) or {}

    style = compute_effective_style(element, font_info)
    font_code = style["font_code"]
    font_size = style["font_size"]
    color_rgb = hex_to_rgb(style["color"])
    replacement_text = str(target.get("replacement_substring", ""))

    orig_width = 0.0
    if target_bbox and len(target_bbox) >= 4:
        orig_width = float(target_bbox[2] - target_bbox[0])

    # 1. TYPE A: Original PDF background preservation via transparent redaction fill=None
    if not skip_redaction and op in ("replace", "delete") and target_bbox and len(target_bbox) >= 4:
        rect = fitz.Rect(*target_bbox)
        page.add_redact_annot(rect, fill=fill_color)
        page.apply_redactions()

    # 2. TYPE B: User-requested text highlight / background
    if style["user_bg_enabled"] and style["user_bg_color"] and target_bbox and len(target_bbox) >= 4:
        user_bg_rgb = hex_to_rgb(style["user_bg_color"])
        bg_rect = fitz.Rect(
            target_bbox[0] - 1.0,
            target_bbox[1] - 1.0,
            target_bbox[2] + 1.0,
            target_bbox[3] + 1.0,
        )
        page.draw_rect(bg_rect, color=None, fill=user_bg_rgb)

    ins_x = 0.0
    if op == "insert":
        ins_pt = target.get("insertion_point")
        if ins_pt and len(ins_pt) >= 2:
            ins_x, baseline_y = float(ins_pt[0]), float(ins_pt[1])
        elif target_bbox and len(target_bbox) >= 4:
            ins_x, baseline_y = float(target_bbox[0]), float(target_bbox[1])
    elif target_bbox and len(target_bbox) >= 4:
        ins_x = float(target_bbox[0])

    rendered_width = 0.0
    warnings = []
    font_scaled = False
    collides = False

    # 3. Geometrical Neighbor Collision Detection
    next_word_x = target.get("next_word_x")
    if next_word_x is not None:
        available_width = max(0.0, float(next_word_x) - ins_x - 2.0)
    elif target_bbox and len(target_bbox) >= 4:
        available_width = max(orig_width, float(page.rect.width) - ins_x - 20.0)
    else:
        available_width = max(orig_width, 100.0)

    if replacement_text and op in ("replace", "insert"):
        unscaled_width = fitz.get_text_length(replacement_text, fontname=font_code, fontsize=font_size)

        if next_word_x is not None and unscaled_width > available_width:
            collides = True
            warnings.append(
                f"Collision warning: replacement text width ({unscaled_width:.1f}pt) "
                f"exceeds available width ({available_width:.1f}pt) to neighboring word."
            )
            scale_ratio = available_width / unscaled_width
            if scale_ratio >= 0.70:
                font_size = font_size * scale_ratio
                font_scaled = True
                warnings.append(f"Font size adjusted to {font_size:.1f}pt to prevent collision.")

        insert_pt = fitz.Point(ins_x, baseline_y)
        if font_file:
            page.insert_text(
                insert_pt,
                replacement_text,
                fontsize=font_size,
                fontfile=font_file,
                color=color_rgb,
            )
        else:
            page.insert_text(
                insert_pt,
                replacement_text,
                fontsize=font_size,
                fontname=font_code,
                color=color_rgb,
            )

        rendered_width = fitz.get_text_length(replacement_text, fontname=font_code, fontsize=font_size)

        # 4. Text decorations (Underline & Strikethrough)
        if style["underline"]:
            u_y = baseline_y + font_size * 0.12
            page.draw_line(
                fitz.Point(ins_x, u_y),
                fitz.Point(ins_x + rendered_width, u_y),
                color=color_rgb,
                width=max(0.8, font_size * 0.06),
            )

        if style["strikethrough"]:
            s_y = baseline_y - font_size * 0.28
            page.draw_line(
                fitz.Point(ins_x, s_y),
                fitz.Point(ins_x + rendered_width, s_y),
                color=color_rgb,
                width=max(0.8, font_size * 0.06),
            )

    return {
        "operation": op,
        "original_width": orig_width,
        "rendered_width": rendered_width,
        "width_diff": rendered_width - orig_width,
        "available_width": available_width,
        "collides": collides,
        "font_used": font_code if not font_file else font_file,
        "font_scaled": font_scaled,
        "baseline_y": baseline_y,
        "insertion_x": ins_x,
        "style": style,
        "warnings": warnings,
    }

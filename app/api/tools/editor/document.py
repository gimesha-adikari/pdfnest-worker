from __future__ import annotations

import json
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

import pymupdf as fitz
import psutil
import pytesseract
from PIL import Image


def int_color_to_hex(color_int: int) -> str:
    try:
        r = (color_int >> 16) & 255
        g = (color_int >> 8) & 255
        b = color_int & 255
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#000000"


def hex_to_rgb(hex_str: str) -> Tuple[float, float, float]:
    try:
        s = (hex_str or "#ffffff").strip().lstrip("#")
        if len(s) != 6:
            s = "ffffff"
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
        return (r, g, b)
    except Exception:
        return (1.0, 1.0, 1.0)


def pixmap_to_image(pix: fitz.Pixmap) -> Image.Image:
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def sample_background_hex(page: fitz.Page, rect: fitz.Rect) -> str:
    samples: List[Tuple[int, int, int]] = []
    pad = 2.0
    candidate_rects = [
        fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x0 - 1, rect.y0 - 1),
        fitz.Rect(rect.x1 + 1, rect.y0 - pad, rect.x1 + pad, rect.y0 - 1),
        fitz.Rect(rect.x0 - pad, rect.y1 + 1, rect.x0 - 1, rect.y1 + pad),
        fitz.Rect(rect.x1 + 1, rect.y1 + 1, rect.x1 + pad, rect.y1 + pad),
    ]

    for clip in candidate_rects:
        try:
            clip = clip & page.rect
            if clip.is_empty or clip.width <= 0 or clip.height <= 0:
                continue
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(1, 1), alpha=False)
            if not pix.samples or len(pix.samples) < 3:
                continue
            samples.append((pix.samples[0], pix.samples[1], pix.samples[2]))
        except Exception:
            continue

    if not samples:
        return "#ffffff"

    r = round(sum(c[0] for c in samples) / len(samples))
    g = round(sum(c[1] for c in samples) / len(samples))
    b = round(sum(c[2] for c in samples) / len(samples))
    return f"#{r:02x}{g:02x}{b:02x}"


def group_words_by_line(word_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not word_items:
        return []

    heights = [max(1.0, float(w["rect"].height)) for w in word_items]
    line_tol = max(4.0, median(heights) * 0.75)
    lines: List[Dict[str, Any]] = []

    for item in sorted(word_items, key=lambda w: (w["rect"].y0, w["rect"].x0)):
        rect = item["rect"]
        placed = False
        for line in lines:
            if abs(rect.y0 - line["y0"]) <= line_tol or abs(rect.y1 - line["y1"]) <= line_tol:
                line["items"].append(item)
                line["x0"] = min(line["x0"], rect.x0)
                line["x1"] = max(line["x1"], rect.x1)
                line["y0"] = min(line["y0"], rect.y0)
                line["y1"] = max(line["y1"], rect.y1)
                placed = True
                break
        if not placed:
            lines.append({"items": [item], "x0": rect.x0, "x1": rect.x1, "y0": rect.y0, "y1": rect.y1})
    return lines


def ocr_words_for_page(page: fitz.Page, zoom: float = 2.0, lang: str = "eng") -> List[Dict[str, Any]]:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = pixmap_to_image(pix)

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, lang=lang)
    items: List[Dict[str, Any]] = []
    n = len(data.get("text", []))

    for i in range(n):
        text = str(data["text"][i]).strip()
        if not text:
            continue
        conf_raw = data.get("conf", ["-1"])[i]
        try:
            conf = float(conf_raw)
        except Exception:
            conf = -1.0
        if conf < 0:
            continue
        left = float(data["left"][i]) / zoom
        top = float(data["top"][i]) / zoom
        width = float(data["width"][i]) / zoom
        height = float(data["height"][i]) / zoom
        rect = fitz.Rect(left, top, left + width, top + height)
        items.append({"rect": rect, "text": text, "conf": conf})

    return items


def extract_native_page(page: fitz.Page, page_number: int) -> Dict[str, Any]:
    page_rect = page.rect
    text_dict = page.get_text("dict") or {}
    blocks = text_dict.get("blocks", []) or []
    native_words = page.get_text("words") or []

    elements: List[Dict[str, Any]] = []
    text_block_count = 0
    image_block_count = 0

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == 1:
            image_block_count += 1
            continue
        bbox = block.get("bbox")
        if btype != 0 or not bbox or len(bbox) < 4:
            continue
        lines = block.get("lines", []) or []
        for line in lines:
            line_bbox = line.get("bbox")
            if not line_bbox or len(line_bbox) < 4:
                continue
            lx0, ly0, lx1, ly1 = line_bbox
            line_text = ""
            font_size = 12
            font_name = "sans-serif"
            text_color_hex = "#000000"
            spans = line.get("spans", []) or []
            for span in spans:
                span_text = span.get("text", "")
                if span_text:
                    line_text += span_text
                font_size = span.get("size", font_size)
                font_name = span.get("font", font_name)
                if "color" in span:
                    try:
                        text_color_hex = int_color_to_hex(int(span["color"]))
                    except Exception:
                        text_color_hex = "#000000"
            line_text = line_text.strip()
            if not line_text:
                continue
            rect = fitz.Rect(lx0, ly0, lx1, ly1)
            elements.append(
                {
                    "text": line_text,
                    "x": rect.x0,
                    "y": rect.y0,
                    "width": rect.width,
                    "height": rect.height,
                    "size": font_size,
                    "font": font_name,
                    "bg_color": sample_background_hex(page, rect),
                    "text_color": text_color_hex,
                }
            )
            text_block_count += 1

    word_count = len(native_words)
    if word_count == 0 and image_block_count == 0:
        kind = "blank"
    elif word_count == 0 and image_block_count > 0:
        kind = "scanned"
    elif word_count > 0 and image_block_count == 0:
        kind = "text"
    else:
        kind = "mixed"

    return {
        "page_num": page_number,
        "width": page_rect.width,
        "height": page_rect.height,
        "elements": elements,
        "kind": kind,
        "has_selectable_text": word_count > 0,
        "word_count": word_count,
        "text_block_count": text_block_count,
        "image_block_count": image_block_count,
    }


def extract_ocr_page(page: fitz.Page, page_number: int) -> Dict[str, Any]:
    page_rect = page.rect
    word_items = ocr_words_for_page(page)
    lines = group_words_by_line(word_items)
    elements: List[Dict[str, Any]] = []

    for line in lines:
        items = sorted(line["items"], key=lambda w: w["rect"].x0)
        text = " ".join(str(w["text"]) for w in items).strip()
        if not text:
            continue
        rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
        if rect.width <= 0 or rect.height <= 0:
            continue
        font_size = max(8.0, min(32.0, rect.height * 0.80))
        elements.append(
            {
                "text": text,
                "x": rect.x0,
                "y": rect.y0,
                "width": rect.width,
                "height": rect.height,
                "size": font_size,
                "font": "OCR",
                "bg_color": sample_background_hex(page, rect),
                "text_color": "#000000",
            }
        )

    return {
        "page_num": page_number,
        "width": page_rect.width,
        "height": page_rect.height,
        "elements": elements,
        "kind": "scanned" if elements else "blank",
        "is_ocr": True,
        "has_selectable_text": False,
        "word_count": 0,
        "text_block_count": 0,
        "image_block_count": 0,
    }


def extract_document(input_path: str, password: Optional[str] = None) -> Dict[str, Any]:
    doc = fitz.open(input_path)
    try:
        if doc.needs_pass:
            if not password:
                raise RuntimeError("PDF is password protected but no password was provided")
            if doc.authenticate(password) <= 0:
                raise RuntimeError("Invalid PDF password")

        pages: List[Dict[str, Any]] = []
        for i in range(doc.page_count):
            page = doc[i]
            native_words = page.get_text("words") or []
            if len(native_words) > 0:
                pages.append(extract_native_page(page, i + 1))
            else:
                pages.append(extract_ocr_page(page, i + 1))

        return {"success": True, "pages": pages}
    finally:
        doc.close()


def mask_and_rasterize_page(page: fitz.Page, elements: List[Dict[str, Any]]) -> fitz.Pixmap:
    for element in elements:
        try:
            x0 = float(element.get("x", 0))
            y0 = float(element.get("y", 0))
            w = float(element.get("width", 0))
            h = float(element.get("height", 0))
            bg_hex = element.get("bg_color", "#ffffff")
            if w <= 0 or h <= 0:
                continue
            rect = fitz.Rect(x0 - 1, y0 - 1, x0 + w + 2, y0 + h + 1)
            color_rgb = hex_to_rgb(bg_hex)
            page.draw_rect(rect, color=color_rgb, fill=color_rgb, overlay=True)
        except Exception:
            continue
    return page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)


def compile_document(original_pdf_path: str, output_pdf_path: str, pages_json_path: str) -> None:
    with open(pages_json_path, "r", encoding="utf-8") as f:
        layout_data = json.load(f)

    upright_path = layout_data.get("upright_tracker") or original_pdf_path
    orig_doc = fitz.open(original_pdf_path)
    source_doc = fitz.open(upright_path)
    output_doc = fitz.open()

    try:
        rotations = [p.rotation for p in orig_doc]
        pages = layout_data.get("pages", [])
        process = psutil.Process()
        print(process.memory_info().rss / 1024 / 1024)

        for page_idx, page_data in enumerate(pages):
            if page_idx >= len(source_doc):
                continue
            source_page = source_doc[page_idx]
            elements = page_data.get("elements", []) or []
            pix = mask_and_rasterize_page(source_page, elements)
            new_page = output_doc.new_page(width=source_page.rect.width, height=source_page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pix)

            for element in elements:
                text_val = str(element.get("text", "")).strip()
                if not text_val:
                    continue
                try:
                    x0 = float(element.get("x", 0))
                    y0 = float(element.get("y", 0))
                    w = float(element.get("width", 0))
                    h = float(element.get("height", 0))
                    font_size = float(element.get("size", 11))
                    text_color_hex = element.get("text_color", "#000000")
                    color_rgb = hex_to_rgb(text_color_hex)
                    rect = fitz.Rect(x0, y0, x0 + w, y0 + h)
                    try:
                        rc = new_page.insert_textbox(rect, text_val, fontsize=font_size, fontname="helv", color=color_rgb, align=0)
                        process = psutil.Process()
                        print(process.memory_info().rss / 1024 / 1024)
                        if rc < 0:
                            pt = fitz.Point(x0, y0 + (h * 0.85))
                            new_page.insert_text(pt, text_val, fontsize=font_size, fontname="helv", color=color_rgb)
                    except Exception:
                        pt = fitz.Point(x0, y0 + (h * 0.85))
                        new_page.insert_text(pt, text_val, fontsize=font_size, fontname="helv", color=color_rgb)
                except Exception:
                    continue

            if page_idx < len(rotations):
                new_page.set_rotation(rotations[page_idx])

        output_doc.save(output_pdf_path)
    finally:
        output_doc.close()
        source_doc.close()
        orig_doc.close()

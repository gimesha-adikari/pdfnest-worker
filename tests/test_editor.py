from __future__ import annotations

import json
import tempfile
from unittest.mock import MagicMock, patch

import fitz
import pytest

from app.api.tools.editor.document import compile_document, is_element_dirty


def test_is_element_dirty_cases():
    # 1. current text == original text -> NOT dirty
    assert is_element_dirty({"text": "Hello", "original_text": "Hello"}) is False

    # 2. current text != original text -> dirty
    assert is_element_dirty({"text": "Hello World", "original_text": "Hello"}) is True

    # 3. original text missing/null
    assert is_element_dirty({"text": "Hello", "original_text": None}) is True
    assert is_element_dirty({"text": "Hello"}) is True

    # 4. empty original text and empty current text -> NOT dirty
    assert is_element_dirty({"text": "", "original_text": ""}) is False
    assert is_element_dirty({"text": None, "original_text": None}) is False

    # 5. empty original text and non-empty current text -> dirty
    assert is_element_dirty({"text": "New Text", "original_text": ""}) is True
    assert is_element_dirty({"text": "New Text", "original_text": None}) is True

    # 6. whitespace differences -> dirty (no accidental stripping/normalizing)
    assert is_element_dirty({"text": "Hello ", "original_text": "Hello"}) is True
    assert is_element_dirty({"text": "Hello", "original_text": " Hello"}) is True

    # Non-dict safe fallback
    assert is_element_dirty("not a dict") is False


def create_sample_multi_line_pdf() -> str:
    """Create a sample 3-line PDF file and return its file path."""
    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Line One Original")
    page.insert_text((50, 100), "Line Two Original")
    page.insert_text((50, 150), "Line Three Original")
    doc.save(temp_pdf.name)
    doc.close()
    return temp_pdf.name


def test_compile_document_skips_unchanged_elements():
    """Regression test: Unchanged elements must produce zero redaction or insertion calls."""
    sample_pdf_path = create_sample_multi_line_pdf()
    output_pdf_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    pages_json_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

    # Layout where only Line 2 is modified; Line 1 & Line 3 are unchanged.
    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "width": 600,
                "height": 800,
                "elements": [
                    {
                        "text": "Line One Original",
                        "original_text": "Line One Original",
                        "x": 50,
                        "y": 50,
                        "width": 100,
                        "height": 15,
                        "size": 12,
                        "font": "tiro",
                    },
                    {
                        "text": "Line Two MODIFIED",
                        "original_text": "Line Two Original",
                        "x": 50,
                        "y": 100,
                        "width": 100,
                        "height": 15,
                        "size": 12,
                        "font": "tiro",
                    },
                    {
                        "text": "Line Three Original",
                        "original_text": "Line Three Original",
                        "x": 50,
                        "y": 150,
                        "width": 100,
                        "height": 15,
                        "size": 12,
                        "font": "tiro",
                    },
                ],
            }
        ]
    }

    with open(pages_json_path, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    redact_mock = MagicMock()
    text_mock = MagicMock(return_value=1)

    with patch.object(fitz.Page, "add_redact_annot", redact_mock), patch.object(
        fitz.Page, "insert_text", text_mock
    ):
        compile_document(sample_pdf_path, output_pdf_path, pages_json_path)

        # Exactly 1 element was dirty ("Line Two MODIFIED"), so add_redact_annot and insert_text
        # must be called EXACTLY 1 time.
        assert redact_mock.call_count == 1
        assert text_mock.call_count == 1


def test_compile_document_all_unchanged_produces_zero_modifications():
    """Regression test: If all elements are unchanged, zero redactions/insertions occur."""
    sample_pdf_path = create_sample_multi_line_pdf()
    output_pdf_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    pages_json_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

    layout_data = {
        "pages": [
            {
                "page_num": 1,
                "width": 600,
                "height": 800,
                "elements": [
                    {
                        "text": "Line One Original",
                        "original_text": "Line One Original",
                        "x": 50,
                        "y": 50,
                        "width": 100,
                        "height": 15,
                    },
                    {
                        "text": "Line Two Original",
                        "original_text": "Line Two Original",
                        "x": 50,
                        "y": 100,
                        "width": 100,
                        "height": 15,
                    },
                    {
                        "text": "Line Three Original",
                        "original_text": "Line Three Original",
                        "x": 50,
                        "y": 150,
                        "width": 100,
                        "height": 15,
                    },
                ],
            }
        ]
    }

    with open(pages_json_path, "w", encoding="utf-8") as f:
        json.dump(layout_data, f)

    redact_mock = MagicMock()
    textbox_mock = MagicMock(return_value=1)

    with patch.object(fitz.Page, "add_redact_annot", redact_mock), patch.object(
        fitz.Page, "insert_textbox", textbox_mock
    ):
        compile_document(sample_pdf_path, output_pdf_path, pages_json_path)

        assert redact_mock.call_count == 0
        assert textbox_mock.call_count == 0


def test_compute_text_diff_all_cases_phase2_5():
    from app.api.tools.editor.document import compute_text_diff

    # A. One word replacement: full source word 'beautiful' -> 'wonderful'
    diffA = compute_text_diff("Hello beautiful world", "Hello wonderful world")
    assert len(diffA) == 1
    assert diffA[0]["operation"] == "replace"
    assert diffA[0]["original_substring"] == "beautiful"
    assert diffA[0]["replacement_substring"] == "wonderful"
    assert diffA[0]["original_start"] == 6
    assert diffA[0]["original_end"] == 15

    # B. Word replacement: full source word 'production-ready' -> 'production-grade'
    diffB = compute_text_diff("Built a production-ready PDF platform", "Built a production-grade PDF platform")
    assert len(diffB) == 1
    assert diffB[0]["operation"] == "replace"
    assert diffB[0]["original_substring"] == "production-ready"
    assert diffB[0]["replacement_substring"] == "production-grade"
    assert diffB[0]["original_start"] == 8
    assert diffB[0]["original_end"] == 24

    # C. Word insertion: 'Hello world' -> 'Hello wonderful world'
    diffC = compute_text_diff("Hello world", "Hello wonderful world")
    assert len(diffC) == 1
    assert diffC[0]["operation"] == "insert"
    assert diffC[0]["original_substring"] == ""
    assert diffC[0]["replacement_substring"] == "wonderful"
    assert diffC[0]["original_start"] == 6
    assert diffC[0]["original_end"] == 5

    # D. Word deletion: 'Hello wonderful world' -> 'Hello world'
    diffD = compute_text_diff("Hello wonderful world", "Hello world")
    assert len(diffD) == 1
    assert diffD[0]["operation"] == "delete"
    assert diffD[0]["original_substring"] == "wonderful"
    assert diffD[0]["replacement_substring"] == ""
    assert diffD[0]["original_start"] == 6
    assert diffD[0]["original_end"] == 15

    # E. Ambiguous repeated word: 'Java Java Java' -> 'Java Python Java'
    diffE = compute_text_diff("Java Java Java", "Java Python Java")
    assert len(diffE) == 1
    assert diffE[0]["operation"] == "replace"
    assert diffE[0]["original_substring"] == "Java"
    assert diffE[0]["replacement_substring"] == "Python"
    assert diffE[0]["original_start"] == 5
    assert diffE[0]["original_end"] == 9

    # F. Insertion at start: 'Developer' -> 'Senior Developer'
    diffF = compute_text_diff("Developer", "Senior Developer")
    assert len(diffF) == 1
    assert diffF[0]["operation"] == "insert"
    assert diffF[0]["original_substring"] == ""
    assert diffF[0]["replacement_substring"] == "Senior"
    assert diffF[0]["original_start"] == 0

    # G. Truncation / shortening: 'Developer' -> 'Dev'
    diffG = compute_text_diff("Developer", "Dev")
    assert len(diffG) == 1
    assert diffG[0]["operation"] == "replace"
    assert diffG[0]["original_substring"] == "Developer"
    assert diffG[0]["replacement_substring"] == "Dev"
    assert diffG[0]["original_start"] == 0
    assert diffG[0]["original_end"] == 9

    # H. Character typo inside word: 'beautiful' -> 'beautyful'
    diffH = compute_text_diff("beautiful", "beautyful")
    assert len(diffH) == 1
    assert diffH[0]["operation"] == "replace"
    assert diffH[0]["original_substring"] == "beautiful"
    assert diffH[0]["replacement_substring"] == "beautyful"
    assert diffH[0]["original_start"] == 0
    assert diffH[0]["original_end"] == 9

    # Sub-word punctuation precision: 'Engineer.' -> 'Engineer,'
    diffPunct = compute_text_diff("Engineer.", "Engineer,")
    assert len(diffPunct) == 1
    assert diffPunct[0]["operation"] == "replace"
    assert diffPunct[0]["original_substring"] == "."
    assert diffPunct[0]["replacement_substring"] == ","


def test_diff_and_geometry_mapping_with_real_pdf():
    """Real PyMuPDF geometry test: verifies target bbox matches complete word 'beautiful', not 'beauti'."""
    from app.api.tools.editor.document import resolve_surgical_targets

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello beautiful world", fontsize=12)

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello beautiful world",
        "x": 50,
        "y": 40,
        "width": 200,
        "height": 15,
        "size": 12,
        "font": "helv",
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1

    target = targets[0]
    assert target["operation"] == "replace"
    assert target["original_substring"] == "beautiful"
    assert target["replacement_substring"] == "wonderful"
    assert target["granularity"] == "word"
    assert target["confidence"] == "exact"

    # Verify that target bbox covers ALL glyphs of 'beautiful'
    bbox = target["target_bbox"]
    assert bbox[0] > 50  # x0 of 'beautiful' starts AFTER 'Hello '
    assert bbox[2] < 200 # x1 ends BEFORE ' world'
    assert abs(target["baseline_y"] - 50.0) < 1.0


def test_insertion_target_has_no_original_glyph_bbox():
    """Verify insertions have target_bbox = None and valid insertion_point."""
    from app.api.tools.editor.document import resolve_surgical_targets

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 50), "Hello world", fontsize=12)

    element = {
        "text": "Hello wonderful world",
        "original_text": "Hello world",
        "x": 50,
        "y": 40,
        "width": 150,
        "height": 15,
        "size": 12,
    }

    targets = resolve_surgical_targets(page, element)
    assert len(targets) == 1

    target = targets[0]
    assert target["operation"] == "insert"
    assert target["replacement_substring"] == "wonderful"
    assert target["target_bbox"] is None  # Must NOT invent fake bbox for non-existent original text
    assert target["insertion_point"] is not None
    assert target["insertion_point"][0] > 50  # Right edge of 'Hello '


def test_unicode_target_mapping():
    """Verify that multi-script Unicode strings (Sinhala, Tamil, Arabic) diff cleanly without code point corruption."""
    from app.api.tools.editor.document import compute_text_diff

    # Sinhala
    diff_sin = compute_text_diff("සිංහල සටහන", "සිංහල ලේඛනය")
    assert len(diff_sin) == 1
    assert diff_sin[0]["original_substring"] == "සටහන"
    assert diff_sin[0]["replacement_substring"] == "ලේඛනය"

    # Tamil
    diff_tam = compute_text_diff("தமிழ் ஆவணம்", "தமிழ் உரை")
    assert len(diff_tam) == 1
    assert diff_tam[0]["original_substring"] == "ஆவணம்"
    assert diff_tam[0]["replacement_substring"] == "உரை"

    # Arabic
    diff_ara = compute_text_diff("مستند PDF", "تقرير PDF")
    assert len(diff_ara) == 1
    assert diff_ara[0]["original_substring"] == "مستند"
    assert diff_ara[0]["replacement_substring"] == "تقرير"


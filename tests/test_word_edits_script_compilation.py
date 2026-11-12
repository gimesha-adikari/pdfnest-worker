from __future__ import annotations

import hashlib
from io import BytesIO

import pymupdf as fitz
import pytest

from app.core.scanned_word_compiler import compile_scanned_word_edits


PROTOCOL = "platen_word_edits.v1"
SCRIPT_REPLACEMENTS = (
    # Sinhala with virama and ZWJ shaping behavior.
    ("Sinhala", "අධ්‍යාපනය"),
    # Tamil with a virama-consonant cluster and dependent vowel sign.
    ("Tamil", "கல்வி"),
)
SCRIPT_SCAN_REPLACEMENTS = (
    ("Sinhala", "කී"),  # consonant plus dependent vowel sign
    ("Tamil", "கி"),  # consonant plus dependent vowel sign
)
SCRIPT_LONG_SCAN_REPLACEMENTS = (
    ("Sinhala", "අධ්‍යාපනය"),
    ("Tamil", "கல்வியியல்"),
)
SCRIPT_UNCOVERED_CODEPOINTS = (
    ("Sinhala", "ක\u0d80"),
    ("Tamil", "க\u0b80"),
)
IMAGE_ONLY_SCRIPT_REPLACEMENTS = (
    ("Sinhala", "කී"),
    ("Tamil", "கி"),
)


def _source_pdf(path):
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((60, 120), "Ministry          of Education, Higher", fontname="helv", fontsize=18)
    target = next(word for word in page.get_text("words", sort=False) if word[4] == "Ministry")
    bbox = [float(value) for value in target[:4]]
    document.save(path)
    document.close()
    return bbox


def _searchable_source_pdf(path):
    page_width, page_height = 595, 842
    # Render the visible scan from the same source text geometry used for the
    # hidden OCR layer so this fixture is aligned by construction.
    visible = fitz.open()
    visible_page = visible.new_page(width=page_width, height=page_height)
    visible_page.insert_text((60, 80), "Ministry of Education, Higher", fontname="helv", fontsize=18)
    bbox = [float(value) for value in next(word for word in visible_page.get_text("words") if word[4] == "Ministry")[:4]]
    raster = visible_page.get_pixmap(dpi=288, colorspace=fitz.csRGB, alpha=False)
    stream = BytesIO(raster.tobytes("png"))
    visible.close()

    document = fitz.open()
    page = document.new_page(width=page_width, height=page_height)
    page.insert_image(page.rect, stream=stream.getvalue())
    page.insert_text((60, 80), "Ministry of Education, Higher", fontname="helv", fontsize=18, render_mode=3)
    document.save(path)
    document.close()
    return bbox


@pytest.mark.parametrize(("language", "replacement"), SCRIPT_REPLACEMENTS)
def test_complex_script_replacement_has_shaped_appearance_and_exact_unicode_text(
    tmp_path, language, replacement
):
    source = tmp_path / f"{language.lower()}-source.pdf"
    output = tmp_path / f"{language.lower()}-candidate.pdf"
    bbox = _source_pdf(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + "1" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline_digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline_digest,
    }]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    assert output.is_file()
    with fitz.open(output) as candidate:
        words = [str(word[4]) for word in candidate[0].get_text("words", sort=False)]
        assert words == [replacement, "of", "Education,", "Higher"]
        assert candidate[0].search_for(replacement)
        assert "Ministry" not in candidate[0].get_text()
        assert any("Noto Sans" in str(font[3]) for font in candidate.get_page_fonts(0, full=True))
        visual_region = fitz.Rect(bbox[0] - 2, bbox[1] - 12, bbox[0] + 110, bbox[3] + 12)
        pix = candidate[0].get_pixmap(clip=visual_region, dpi=144, colorspace=fitz.csGRAY, alpha=False)
        assert sum(1 for value in pix.samples if value < 100) > 100, "the shaped word must be visible, not only extractable"
        assert result["target_results"][0]["replacement_searchable"] is True
        assert result["target_results"][0]["old_target_hits_at_target"] == 0
        assert result["outside_changed_pixels_by_dpi"] == {"72": 0, "144": 0, "288": 0}


@pytest.mark.parametrize(("language", "replacement"), SCRIPT_SCAN_REPLACEMENTS)
def test_complex_script_replacement_keeps_searchable_scan_text_synchronized(tmp_path, language, replacement):
    source = tmp_path / f"{language.lower()}-scan-source.pdf"
    output = tmp_path / f"{language.lower()}-scan-candidate.pdf"
    bbox = _searchable_source_pdf(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + "2" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline_digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline_digest,
    }]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "EDITED", result
    assert result["target_results"][0]["representation"] == "searchable_scan"
    assert result["target_results"][0]["old_target_hits_at_target"] == 0
    assert result["outside_changed_pixels_by_dpi"] == {"72": 0, "144": 0, "288": 0}
    with fitz.open(output) as candidate:
        words = [str(word[4]) for word in candidate[0].get_text("words", sort=False)]
        assert words == [replacement, "of", "Education,", "Higher"]
        assert len(candidate[0].search_for(replacement)) == 1
        assert "Ministry" not in candidate[0].get_text()


@pytest.mark.parametrize(("language", "replacement"), SCRIPT_LONG_SCAN_REPLACEMENTS)
def test_long_complex_script_replacement_is_refused_when_scanned_target_does_not_fit(
    tmp_path, language, replacement
):
    source = tmp_path / f"{language.lower()}-scan-tight-source.pdf"
    output = tmp_path / f"{language.lower()}-scan-tight-candidate.pdf"
    bbox = _searchable_source_pdf(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + "3" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline_digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline_digest,
    }]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result["status"] == "REFUSED", result
    assert result["error_code"] == "REPLACEMENT_DOES_NOT_FIT"
    assert not output.exists()


@pytest.mark.parametrize(("language", "replacement"), SCRIPT_UNCOVERED_CODEPOINTS)
def test_script_replacement_with_missing_font_coverage_is_refused(tmp_path, language, replacement):
    source = tmp_path / f"{language.lower()}-uncovered-source.pdf"
    output = tmp_path / f"{language.lower()}-uncovered-candidate.pdf"
    bbox = _source_pdf(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + "4" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline_digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline_digest,
    }]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "NATIVE_FONT_GLYPH_UNSUPPORTED"}
    assert not output.exists()


@pytest.mark.parametrize(("language", "replacement"), IMAGE_ONLY_SCRIPT_REPLACEMENTS)
def test_image_only_scan_script_replacement_is_refused_without_trusted_typography(tmp_path, language, replacement):
    source = tmp_path / f"{language.lower()}-image-only-source.pdf"
    output = tmp_path / f"{language.lower()}-image-only-candidate.pdf"
    visible = fitz.open()
    visible_page = visible.new_page(width=595, height=842)
    visible_page.insert_text((60, 80), "Ministry of Education, Higher", fontname="helv", fontsize=18)
    bbox = [float(value) for value in next(word for word in visible_page.get_text("words") if word[4] == "Ministry")[:4]]
    image_bytes = visible_page.get_pixmap(dpi=288, alpha=False).tobytes("png")
    visible.close()

    image_only = fitz.open()
    image_only_page = image_only.new_page(width=595, height=842)
    image_only_page.insert_image(image_only_page.rect, stream=image_bytes)
    image_only.save(source)
    image_only.close()

    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + "5" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": replacement,
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": PROTOCOL,
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline_digest,
        "edits": [edit],
    }
    resolved = [{
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline_digest,
    }]

    result = compile_scanned_word_edits(source, output, envelope, resolved, source_sha)

    assert result == {"status": "REFUSED", "error_code": "SCRIPT_SCAN_STYLE_UNRESOLVED"}
    assert not output.exists()


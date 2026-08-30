from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from app.core.ocr_v2 import OCRV2Worker, apply_ocr_markup, select_query, select_regions
from app.core.ocr_v2.contracts import (
    DocumentResult,
    LanguageMetadata,
    OCRToken,
    PageGeometry,
    PageProcessingSource,
    PageResult,
    PageStatus,
    Rect,
    SourceMetadata,
)
from app.core.ocr_v2.markup import MarkupAction, MarkupMode
from app.core.ocr_v2.errors import TextNotFoundError, WordGeometryUnavailableError


def _canonical_result() -> DocumentResult:
    tokens = (
        OCRToken("word-0", "Alpha", Rect(10, 10, 40, 12), line_id="line-0"),
        OCRToken("word-1", "Bravo", Rect(56, 10, 40, 12), line_id="line-0"),
        OCRToken("word-2", "Alpha", Rect(10, 30, 40, 12), line_id="line-1"),
    )
    page = PageResult(
        page_index=0,
        page_id="page-0",
        geometry=PageGeometry(300, 200),
        content_classification="TEXT_NATIVE",  # type: ignore[arg-type]
        processing_source=PageProcessingSource.NATIVE_EXTRACTION,
        status=PageStatus.SUCCESS,
        text="Alpha Bravo\nAlpha",
        tokens=tokens,
        reading_order=tuple(token.id for token in tokens),
        language=LanguageMetadata(("eng",)),
        capabilities=frozenset({"TEXT", "WORD_GEOMETRY", "READING_ORDER"}),
    )
    return DocumentResult("ocr_v2.1", "result", SourceMetadata("fixture", 1), (page,))


def test_shared_matcher_preserves_reading_order_and_repeated_occurrences() -> None:
    selections = select_query(_canonical_result(), "alpha", mode=MarkupMode.SMART)
    assert [selection.word_ids for selection in selections] == [("word-0",), ("word-2",)]

    phrase = select_query(_canonical_result(), "Alpha Bravo", mode=MarkupMode.SMART)
    assert phrase[0].word_ids == ("word-0", "word-1")
    assert phrase[0].reading_order_start == 0


def test_shared_matcher_fails_closed_for_missing_phrase() -> None:
    try:
        select_query(_canonical_result(), "missing", mode=MarkupMode.SMART)
    except TextNotFoundError:
        pass
    else:
        raise AssertionError("missing text must not create an annotation")


def test_shared_region_selection_uses_canonical_words_and_reading_order() -> None:
    selections = select_regions(_canonical_result(), [{"page": 1, "x": 5, "y": 5, "width": 100, "height": 25}], mode=MarkupMode.SMART)
    assert len(selections) == 1
    assert selections[0].word_ids == ("word-0", "word-1")
    assert selections[0].reading_order_start == 0
    assert selections[0].reading_order_end == 1


def test_shared_matcher_requires_genuine_word_geometry() -> None:
    page = _canonical_result().pages[0]
    no_geometry = PageResult(
        page_index=page.page_index,
        page_id=page.page_id,
        geometry=page.geometry,
        content_classification=page.content_classification,
        processing_source=page.processing_source,
        status=page.status,
        text=page.text,
        reading_order=(),
        capabilities=frozenset({"TEXT"}),
    )
    result = DocumentResult("ocr_v2.1", "no-geometry", SourceMetadata("fixture", 1), (no_geometry,))
    try:
        select_query(result, "Alpha", mode=MarkupMode.SMART)
    except WordGeometryUnavailableError:
        pass
    else:
        raise AssertionError("automatic OCR-aware selection must fail closed without word geometry")


def test_mixed_page_uses_one_canonical_source_without_duplicate_matches() -> None:
    page = _canonical_result().pages[0]
    mixed = PageResult(
        page_index=page.page_index,
        page_id=page.page_id,
        geometry=page.geometry,
        content_classification="MIXED",  # type: ignore[arg-type]
        processing_source=PageProcessingSource.HYBRID,
        status=page.status,
        text=page.text,
        tokens=page.tokens,
        reading_order=page.reading_order,
        language=page.language,
        capabilities=page.capabilities,
    )
    result = DocumentResult("ocr_v2.1", "mixed", SourceMetadata("fixture", 1), (mixed,))
    selections = select_query(result, "Alpha", mode=MarkupMode.SMART)
    assert len(selections) == 2
    assert {selection.source_type.value for selection in selections} == {"hybrid"}
    assert all(selection.provenance == () for selection in selections)


def _scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (900, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
    draw.text((80, 150), "Markup Alpha Bravo", font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    document = fitz.open()
    document.new_page(width=432, height=240).insert_image(fitz.Rect(0, 0, 432, 240), stream=stream.getvalue())
    document.save(path)
    document.close()


def test_ocr_markup_uses_canonical_words_and_writes_real_annotation(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    output = tmp_path / "highlight.pdf"
    _scanned_pdf(source)

    result = apply_ocr_markup(
        source,
        output,
        action=MarkupAction.HIGHLIGHT,
        query="Markup Alpha",
        language="eng",
        mode=MarkupMode.SMART,
    )

    assert result.action is MarkupAction.HIGHLIGHT
    assert result.selections
    with fitz.open(output) as document:
        page = document[0]
        annotation = page.first_annot
        assert annotation is not None
        assert annotation.type[1] == "Highlight"
        assert page.get_images(full=True)


def test_all_markup_actions_write_their_specific_annotation_type(tmp_path: Path) -> None:
    source = tmp_path / "native.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((20, 80), "Alpha Bravo", fontsize=24)
    document.save(source)
    document.close()

    expected = {
        MarkupAction.HIGHLIGHT: "Highlight",
        MarkupAction.UNDERLINE: "Underline",
        MarkupAction.STRIKEOUT: "StrikeOut",
    }
    for action, annotation_name in expected.items():
        output = tmp_path / f"{action.value}.pdf"
        execution = apply_ocr_markup(source, output, action=action, query="Alpha Bravo", mode=MarkupMode.NATIVE)
        assert execution.selections
        with fitz.open(output) as marked:
            page = marked[0]
            annotation = page.first_annot
            assert annotation is not None
            assert annotation.type[1] == annotation_name


def test_native_rotation_preserves_page_rotation_and_annotation_geometry(tmp_path: Path) -> None:
    source = tmp_path / "rotated.pdf"
    output = tmp_path / "rotated-marked.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((20, 80), "Rotated Alpha", fontsize=24)
    page.set_rotation(90)
    document.save(source)
    document.close()

    apply_ocr_markup(source, output, action=MarkupAction.UNDERLINE, query="Rotated Alpha", mode=MarkupMode.NATIVE)
    with fitz.open(output) as marked:
        page = marked[0]
        assert page.rotation == 90
        annotation = page.first_annot
        assert annotation is not None
        assert annotation.type[1] == "Underline"
        assert list(page.annots())

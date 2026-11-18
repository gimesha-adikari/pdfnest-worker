from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf as fitz
import pytest

from app.core.scanned_word_compiler import compile_scanned_word_edits
from tests.test_word_edits_script_compilation import _source_pdf


def _request(source: Path, bbox: list[float]):
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline = "sha256:" + "1" * 64
    edit = {
        "page_num": 1,
        "element_id": "line-1",
        "word_id": "word-1",
        "expected_original_text": "Ministry",
        "replacement_text": "hello",
        "style_override": None,
        "fit_policy": "preserve_neighbors",
    }
    envelope = {
        "edit_protocol": "platen_word_edits.v1",
        "source_tracker": "source-key",
        "extraction_job_id": "extract-1",
        "baseline_digest": baseline,
        "edits": [edit],
    }
    target = {
        "edit": edit,
        "bbox": bbox,
        "page_size": [595.0, 842.0],
        "source_sha256": source_sha,
        "baseline_digest": baseline,
    }
    return source_sha, envelope, target


@pytest.mark.parametrize(
    ("user_password", "permissions", "expected_error"),
    [
        ("synthetic-user-password", fitz.PDF_PERM_MODIFY, "PROTECTED_SOURCE_UNSUPPORTED"),
        ("", 0, "PROTECTED_SOURCE_PERMISSION_DENIED"),
        (
            "",
            fitz.PDF_PERM_ACCESSIBILITY
            | fitz.PDF_PERM_PRINT
            | fitz.PDF_PERM_COPY
            | fitz.PDF_PERM_MODIFY,
            "PROTECTED_SOURCE_UNSUPPORTED",
        ),
    ],
)
def test_word_edit_refuses_encrypted_source_without_supported_restoration(
    tmp_path, user_password, permissions, expected_error
):
    plain = tmp_path / "plain.pdf"
    protected = tmp_path / "protected.pdf"
    output = tmp_path / "candidate.pdf"
    bbox = _source_pdf(plain)
    with fitz.open(plain) as document:
        document.save(
            protected,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="synthetic-owner-password",
            user_pw=user_password,
            permissions=permissions,
        )
    source_sha, envelope, target = _request(protected, bbox)

    result = compile_scanned_word_edits(protected, output, envelope, [target], source_sha)

    assert result["status"] == "REFUSED"
    assert result["error_code"] == expected_error
    assert not output.exists()


def test_word_edit_refuses_pdf_with_signature_field_before_mutation(tmp_path):
    plain = tmp_path / "plain.pdf"
    protected = tmp_path / "signature-field.pdf"
    output = tmp_path / "candidate.pdf"
    bbox = _source_pdf(plain)
    with fitz.open(plain) as document:
        widget = fitz.Widget()
        widget.field_name = "SyntheticSignature"
        widget.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
        widget.rect = fitz.Rect(300, 300, 450, 350)
        document[0].add_widget(widget)
        document.save(protected, garbage=4, deflate=True)
    with fitz.open(protected) as document:
        assert document.get_sigflags() > 0
    source_sha, envelope, target = _request(protected, bbox)

    result = compile_scanned_word_edits(protected, output, envelope, [target], source_sha)

    assert result["status"] == "REFUSED"
    assert result["error_code"] == "SIGNED_SOURCE_UNSUPPORTED"
    assert not output.exists()

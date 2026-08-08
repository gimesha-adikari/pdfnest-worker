from __future__ import annotations

import io
import os
from unittest.mock import patch

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def create_sample_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "CONFIDENTIAL DOCUMENT FOR TEST")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_redact_success_cleans_up_input():
    pdf_bytes = create_sample_pdf()

    response = client.post(
        "/api/v1/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"keywords": "CONFIDENTIAL", "boxes": "[]"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_redact_failure_cleans_up_both_temp_files():
    pdf_bytes = create_sample_pdf()

    # Mock redact_pdf to raise an exception
    with patch("app.api.tools.redact.router.redact_pdf", side_effect=ValueError("Simulated processing error")):
        response = client.post(
            "/api/v1/redact",
            files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            data={"keywords": "CONFIDENTIAL", "boxes": "[]"},
        )

        assert response.status_code == 500
        assert "Simulated processing error" in response.json()["detail"]

    # Verify no orphan pdfnest-redact- temp files left in tempdir
    import tempfile
    temp_dir = tempfile.gettempdir()
    leftover = [
        f for f in os.listdir(temp_dir)
        if f.startswith("pdfnest-redact-")
    ]
    assert len(leftover) == 0, f"Found leaked temp files: {leftover}"

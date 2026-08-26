from __future__ import annotations

import io

import fitz
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from tests.test_security import generate_headers


client = TestClient(app)


def create_blank_pdf() -> bytes:
    document = fitz.open()
    document.new_page(width=300, height=400)
    document.new_page(width=300, height=400)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def create_signature_png() -> bytes:
    image = Image.new("RGBA", (12, 8), (255, 0, 0, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_sign_inserts_owned_signature_at_worker_coordinates():
    response = client.post(
        "/api/v1/sign",
        files={
            "file": ("input.pdf", io.BytesIO(create_blank_pdf()), "application/pdf"),
            "signature": ("signature.png", io.BytesIO(create_signature_png()), "image/png"),
        },
        data={"stamps": '[{"page":1,"x":20,"y":30,"width":120,"height":60}]'},
        headers=generate_headers("POST", "/api/v1/sign"),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"

    document = fitz.open(stream=response.content, filetype="pdf")
    try:
        assert len(document[0].get_images(full=True)) == 1
        assert document[1].get_images(full=True) == []
        image_xref = document[0].get_images(full=True)[0][0]
        rect = document[0].get_image_rects(image_xref)[0]
        # PyMuPDF preserves the source image aspect ratio inside the requested box.
        assert rect == fitz.Rect(35, 30, 125, 90)
    finally:
        document.close()


def test_sign_ignores_stamps_outside_document_pages():
    response = client.post(
        "/api/v1/sign",
        files={
            "file": ("input.pdf", io.BytesIO(create_blank_pdf()), "application/pdf"),
            "signature": ("signature.png", io.BytesIO(create_signature_png()), "image/png"),
        },
        data={"stamps": '[{"page":3,"x":20,"y":30,"width":120,"height":60}]'},
        headers=generate_headers("POST", "/api/v1/sign"),
    )

    assert response.status_code == 200
    document = fitz.open(stream=response.content, filetype="pdf")
    try:
        assert all(page.get_images(full=True) == [] for page in document)
    finally:
        document.close()

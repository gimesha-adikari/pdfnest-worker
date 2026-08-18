from __future__ import annotations

import io
from fastapi.testclient import TestClient
import fitz
from app.main import app
from tests.test_security import generate_headers

client = TestClient(app)


def _create_sample_pdf_bytes(num_pages: int = 3) -> bytes:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), f"Sample Page {i + 1} Content", fontsize=20)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_render_session_lifecycle():
    pdf_bytes = _create_sample_pdf_bytes(3)

    # 1. Create Session
    headers1 = generate_headers("POST", "/api/v1/render/sessions")
    res_session = client.post(
        "/api/v1/render/sessions",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        headers=headers1,
    )
    assert res_session.status_code == 200
    data = res_session.json()
    assert "session_id" in data
    assert data["page_count"] == 3
    session_id = data["session_id"]

    # 2. Duplicate Session Creation (SHA256 Deduplication test)
    headers2 = generate_headers("POST", "/api/v1/render/sessions")
    res_session_dup = client.post(
        "/api/v1/render/sessions",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        headers=headers2,
    )
    assert res_session_dup.status_code == 200
    assert res_session_dup.json()["session_id"] == session_id

    # 3. Render Session Pages (Pages 1, 2, 3)
    for page_num in (1, 2, 3):
        path = f"/api/v1/render/sessions/{session_id}/page/{page_num}?dpi=144"
        headers_page = generate_headers("GET", path)
        res_page = client.get(path, headers=headers_page)
        assert res_page.status_code == 200
        assert res_page.headers["content-type"] == "image/jpeg"
        assert len(res_page.content) > 0

    # 4. Out-of-bounds page request (Page 999) -> expect 400 or 500 error
    path_inv = f"/api/v1/render/sessions/{session_id}/page/999?dpi=144"
    headers_inv = generate_headers("GET", path_inv)
    res_invalid_page = client.get(path_inv, headers=headers_inv)
    assert res_invalid_page.status_code in (400, 500)

    # 5. Request Page for Non-existent Session ID -> expect 404
    path_nf = "/api/v1/render/sessions/invalid_session_id_xyz/page/1?dpi=144"
    headers_nf = generate_headers("GET", path_nf)
    res_not_found = client.get(path_nf, headers=headers_nf)
    assert res_not_found.status_code == 404
    assert res_not_found.json()["detail"]["code"] == "SESSION_NOT_FOUND"

    # 6. Delete Session
    path_del = f"/api/v1/render/sessions/{session_id}"
    headers_del = generate_headers("DELETE", path_del)
    res_delete = client.delete(path_del, headers=headers_del)
    assert res_delete.status_code == 200

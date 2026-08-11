from __future__ import annotations

import io
from fastapi.testclient import TestClient
import fitz
from app.main import app

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
    res_session = client.post(
        "/api/v1/render/sessions",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
    )
    assert res_session.status_code == 200
    data = res_session.json()
    assert "session_id" in data
    assert data["page_count"] == 3
    session_id = data["session_id"]

    # 2. Duplicate Session Creation (SHA256 Deduplication test)
    res_session_dup = client.post(
        "/api/v1/render/sessions",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
    )
    assert res_session_dup.status_code == 200
    assert res_session_dup.json()["session_id"] == session_id

    # 3. Render Session Pages (Pages 1, 2, 3)
    for page_num in (1, 2, 3):
        res_page = client.get(f"/api/v1/render/sessions/{session_id}/page/{page_num}?dpi=144")
        assert res_page.status_code == 200
        assert res_page.headers["content-type"] == "image/jpeg"
        assert len(res_page.content) > 0

    # 4. Out-of-bounds page request (Page 999) -> expect 400 or 500 error
    res_invalid_page = client.get(f"/api/v1/render/sessions/{session_id}/page/999?dpi=144")
    assert res_invalid_page.status_code in (400, 500)

    # 5. Request Page for Non-existent Session ID -> expect 404
    res_not_found = client.get("/api/v1/render/sessions/invalid_session_id_xyz/page/1?dpi=144")
    assert res_not_found.status_code == 404
    assert res_not_found.json()["detail"]["code"] == "SESSION_NOT_FOUND"

    # 6. Delete Session
    res_delete = client.delete(f"/api/v1/render/sessions/{session_id}")
    assert res_delete.status_code == 200
    assert res_delete.json() == {"deleted": True}

    # 7. Verify Page Request After Deletion returns 404
    res_post_delete = client.get(f"/api/v1/render/sessions/{session_id}/page/1?dpi=144")
    assert res_post_delete.status_code == 404

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import time
from fastapi.testclient import TestClient
import fitz
import pytest

from app.main import app
from app.api.tools.render import service as render_service
from app.api.tools.render import session as render_session
from tests.test_security import generate_headers, signed_post

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
    res_session = signed_post(client,
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
    res_session_dup = signed_post(client,
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


def test_render_session_deduplication_is_scoped_and_cleanup_is_independent(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    manager = render_session.RenderSessionManager()
    monkeypatch.setattr(render_session, "SESSION_ROOT", session_root)
    monkeypatch.setattr(render_service, "session_manager", manager)

    pdf_bytes = _create_sample_pdf_bytes(2)
    scope_a = "a" * 64
    scope_b = "b" * 64

    def create_session(scope: str, name: str = "same.pdf") -> dict[str, object]:
        request_path = f"/api/v1/render/sessions?dedup_scope={scope}"
        response = signed_post(
            client,
            request_path,
            files={"file": (name, pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    session_a = create_session(scope_a)
    session_a_again = create_session(scope_a, "same-content-again.pdf")
    session_b = create_session(scope_b)

    invalid_scope_path = "/api/v1/render/sessions?dedup_scope=guest%3Aowner-a"
    invalid_scope = signed_post(
        client,
        invalid_scope_path,
        files={"file": ("same.pdf", pdf_bytes, "application/pdf")},
    )
    assert invalid_scope.status_code == 400

    assert session_a["session_id"] == session_a_again["session_id"]
    assert session_a["session_id"] != session_b["session_id"]
    assert session_a["page_count"] == session_b["page_count"] == 2

    session_a_id = str(session_a["session_id"])
    session_b_id = str(session_b["session_id"])
    assert manager.get(session_a_id).dedup_scope == scope_a
    assert manager.get(session_b_id).dedup_scope == scope_b

    for session_id in (session_a_id, session_b_id):
        request_path = f"/api/v1/render/sessions/{session_id}/page/1?dpi=144"
        response = client.get(request_path, headers=generate_headers("GET", request_path))
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    delete_a_path = f"/api/v1/render/sessions/{session_a_id}"
    delete_a = client.delete(delete_a_path, headers=generate_headers("DELETE", delete_a_path))
    assert delete_a.status_code == 200
    assert not (session_root / session_a_id).exists()

    b_page_path = f"/api/v1/render/sessions/{session_b_id}/page/2?dpi=144"
    b_page_after_a_delete = client.get(b_page_path, headers=generate_headers("GET", b_page_path))
    assert b_page_after_a_delete.status_code == 200
    assert (session_root / session_b_id / "source.pdf").exists()

    delete_b_path = f"/api/v1/render/sessions/{session_b_id}"
    delete_b = client.delete(delete_b_path, headers=generate_headers("DELETE", delete_b_path))
    assert delete_b.status_code == 200
    assert not (session_root / session_b_id).exists()

    different_bytes = _create_sample_pdf_bytes(3)
    different_path = tmp_path / "different.pdf"
    different_path.write_bytes(different_bytes)
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(pdf_bytes)
    different_session = asyncio.run(manager.create(
        file_path=different_path,
        file_size=len(different_bytes),
        sha256=hashlib.sha256(different_bytes).hexdigest(),
        dedup_scope=scope_a,
    ))
    same_content_new_scope = asyncio.run(manager.create(
        file_path=source_path,
        file_size=len(pdf_bytes),
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        dedup_scope=scope_a,
    ))
    try:
        assert different_session.sha256 != same_content_new_scope.sha256
        assert different_session.session_id != same_content_new_scope.session_id
    finally:
        manager.delete(different_session.session_id)
        manager.delete(same_content_new_scope.session_id)


def test_render_session_capacity_and_expiration_remain_bounded(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    manager = render_session.RenderSessionManager()
    monkeypatch.setattr(render_session, "SESSION_ROOT", session_root)
    monkeypatch.setattr(render_session, "MAX_SESSIONS", 2)
    sessions = []
    for pages in (1, 2, 3):
        pdf_bytes = _create_sample_pdf_bytes(pages)
        source_path = tmp_path / f"source-{pages}.pdf"
        source_path.write_bytes(pdf_bytes)
        sessions.append(asyncio.run(manager.create(
            file_path=source_path,
            file_size=len(pdf_bytes),
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            dedup_scope=f"scope-{pages}",
        )))

    evicted, active, expiring = sessions
    assert manager.get_optional(evicted.session_id) is None
    assert not (session_root / evicted.session_id).exists()

    expiring.last_accessed_at = time.time() - render_session.SESSION_TTL_SECONDS - 1
    assert manager.get_optional(expiring.session_id) is None
    assert not (session_root / expiring.session_id).exists()

    assert manager.get(active.session_id).dedup_scope == "scope-2"
    manager.delete(active.session_id)


def test_concurrent_same_scope_creation_reuses_one_session(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    manager = render_session.RenderSessionManager()
    monkeypatch.setattr(render_session, "SESSION_ROOT", session_root)

    pdf_bytes = _create_sample_pdf_bytes(1)
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(pdf_bytes)
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    first = asyncio.run(manager.get_or_create(
        file_path=source_path,
        file_size=len(pdf_bytes),
        sha256=sha256,
        dedup_scope="c" * 64,
    ))
    second = asyncio.run(manager.get_or_create(
        file_path=source_path,
        file_size=len(pdf_bytes),
        sha256=sha256,
        dedup_scope="c" * 64,
    ))

    assert first.session_id == second.session_id
    assert len(manager._sessions) == 1
    manager.delete(first.session_id)


def test_failed_session_creation_cleans_uploaded_temporary_pdf(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    upload_root = tmp_path / "uploads"
    session_root.mkdir()
    upload_root.mkdir()
    manager = render_session.RenderSessionManager()
    monkeypatch.setattr(render_session, "SESSION_ROOT", session_root)
    monkeypatch.setattr(render_service, "session_manager", manager)
    monkeypatch.setattr(render_service.tempfile, "tempdir", str(upload_root))

    request_path = "/api/v1/render/sessions?dedup_scope=" + "d" * 64
    response = signed_post(
        client,
        request_path,
        files={"file": ("malformed.pdf", b"not a PDF", "application/pdf")},
    )

    assert response.status_code in (400, 500)
    assert not [f for f in upload_root.iterdir() if f.name.startswith("pdfnest-session-upload-")]
    assert not list(session_root.iterdir())
    assert not manager._sessions

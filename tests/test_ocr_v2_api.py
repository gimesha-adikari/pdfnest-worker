from __future__ import annotations

import hashlib
import hmac
import asyncio
import io
import time
import uuid

import pymupdf as fitz
from fastapi import FastAPI
import httpx
from PIL import Image, ImageDraw

from app.core.config import settings
from app.api.ocr_v2.router import router
from app.core.security import WorkerAuthMiddleware


app_under_test = FastAPI()
app_under_test.add_middleware(WorkerAuthMiddleware)
app_under_test.include_router(router)


def _post(path: str, **kwargs: object) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app_under_test)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            return await async_client.post(path, **kwargs)

    return asyncio.run(request())


def _get(path: str, **kwargs: object) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app_under_test)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            return await async_client.get(path, **kwargs)

    return asyncio.run(request())


def _headers(method: str, path: str, *, timestamp: int | None = None, nonce: str | None = None, secret: str | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    nonce_value = nonce or f"phase3b-{uuid.uuid4().hex}"
    signed = f"{method}\n{path}\n{ts}\n{nonce_value}"
    signature = hmac.new((secret or settings.worker_shared_secret).encode(), signed.encode(), hashlib.sha256).hexdigest()
    return {"X-Worker-Signature": signature, "X-Worker-Timestamp": ts, "X-Worker-Nonce": nonce_value}


def _native_pdf() -> bytes:
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((40, 80), "Native OCR V2 boundary")
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _scanned_pdf() -> bytes:
    image = Image.new("RGB", (600, 400), "white")
    ImageDraw.Draw(image).text((40, 80), "Scanned OCR V2 123", fill="black")
    image_bytes = io.BytesIO()
    image.save(image_bytes, "PNG")
    document = fitz.open()
    document.new_page(width=300, height=200).insert_image(fitz.Rect(0, 0, 300, 200), stream=image_bytes.getvalue())
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def test_ocr_v2_missing_auth_is_rejected() -> None:
    response = _post("/internal/ocr/v2/text", files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "missing-auth"})
    assert response.status_code == 401


def test_ocr_v2_capabilities_are_product_safe() -> None:
    headers = _headers("GET", "/internal/ocr/v2/capabilities")
    response = _get("/internal/ocr/v2/capabilities", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == "OCR_TEXT_V2"
    assert all(item["code"] and item["name"] for item in payload["languages"])
    assert {item["id"] for item in payload["routing_modes"]} == {"AUTO", "FAST", "QUALITY"}
    assert "model" not in response.text.lower()
    assert "/" not in response.json()["languages"][0]["name"]


def test_ocr_v2_invalid_signature_is_rejected() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text", secret="wrong-secret")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "invalid-signature"})
    assert response.status_code == 401


def test_ocr_v2_expired_request_is_rejected() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text", timestamp=int(time.time()) - 600)
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "expired"})
    assert response.status_code == 401


def test_ocr_v2_replayed_request_is_rejected() -> None:
    nonce = f"phase3b-replay-{uuid.uuid4().hex}"
    headers = _headers("POST", "/internal/ocr/v2/text", nonce=nonce)
    first = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "replay-1", "language": "eng"})
    second = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "replay-2", "language": "eng"})
    assert first.status_code == 200
    assert second.status_code == 401


def test_ocr_v2_valid_authenticated_native_flow() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "native-flow", "language": "eng", "routing_policy": "AUTO"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "SUCCEEDED"
    assert "Native OCR V2 boundary" in payload["text"]
    assert payload["pages"][0]["source"] == "pymupdf_native_extractor"


def test_ocr_v2_sdk_engine_authenticated_native_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_TEXT_ENGINE", "sdk")
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "sdk-native-flow", "language": "eng", "routing_policy": "AUTO"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "SUCCEEDED"
    assert "Native OCR V2 boundary" in payload["text"]
    assert payload["pages"][0]["source"] == "pymupdf_native_extractor"


def test_ocr_v2_quality_policy_reports_explicit_pp_fallback() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("scan.pdf", _scanned_pdf(), "application/pdf")}, data={"request_id": "fallback-flow", "language": "eng", "routing_policy": "QUALITY"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "SUCCEEDED"
    assert any(code.startswith("ENGINE_FALLBACK") for code in payload["warnings"])
    assert "Scanned" in payload["text"] and "OCR" in payload["text"]


def test_ocr_v2_unsupported_profile_is_typed() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "bad-profile", "profile": "SEARCHABLE_PDF_V2"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_INPUT"


def test_ocr_v2_uninstalled_language_is_typed() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "bad-language", "language": "not-a-worker-language", "routing_policy": "FAST"})
    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_LANGUAGE"


def test_ocr_v2_missing_language_is_rejected() -> None:
    headers = _headers("POST", "/internal/ocr/v2/text")
    response = _post("/internal/ocr/v2/text", headers=headers, files={"file": ("native.pdf", _native_pdf(), "application/pdf")}, data={"request_id": "missing-language"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_INPUT"

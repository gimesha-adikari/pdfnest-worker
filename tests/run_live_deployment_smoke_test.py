#!/usr/bin/env python3
"""
PDFNest Studio V2 — Hardened Live Deployment Smoke Test Runner (Remote HTTP)
CLASSIFICATION: LIVE REMOTE/CONTAINER SMOKE TEST
Validates operational health, HMAC authentication, persistent worker pool health,
end-to-end rendering over real TCP network HTTP, and production security gating.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import os
import sys
import time
import uuid
import httpx
from PIL import Image
import pymupdf as fitz


def generate_auth_headers(secret: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    nonce = str(uuid.uuid4())
    timestamp_str = str(time.time())
    method_str = method.upper()

    body_hash = hashlib.sha256(body).hexdigest() if body else ""

    if body_hash:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}\n{body_hash}"
    else:
        string_to_sign = f"{method_str}\n{path}\n{timestamp_str}\n{nonce}"

    sig = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-Worker-Signature": sig,
        "X-Worker-Timestamp": timestamp_str,
        "X-Worker-Nonce": nonce,
    }
    if body_hash:
        headers["X-Worker-Body-Hash"] = body_hash

    return headers


def make_sample_pdf(title: str = "Live Smoke Test Document") -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), f"PDFNest Studio V2 — {title}", fontsize=16)
    page.draw_rect(fitz.Rect(50, 140, 545, 260), color=(0.2, 0.4, 0.8), fill=(0.9, 0.95, 1.0))
    page.insert_text((60, 180), "Live Remote/Container Deployment Verification Payload", fontsize=12)
    page.insert_text((60, 210), f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}", fontsize=10)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def main():
    parser = argparse.ArgumentParser(description="PDFNest Studio V2 Live Remote Deployment Smoke Test")
    parser.add_argument("--url", default=os.getenv("TARGET_URL"), help="Target service base URL (or TARGET_URL env)")
    parser.add_argument("--secret", default=os.getenv("WORKER_SHARED_SECRET"), help="HMAC secret (or WORKER_SHARED_SECRET env)")
    parser.add_argument("--expect-disabled", action="store_true", help="Expect persistent pool to be disabled (rollback mode)")
    parser.add_argument("--expected-workers", type=int, default=None, help="Expected number of configured/healthy workers")
    args = parser.parse_args()

    if not args.url:
        print("ERROR: Target URL must be specified via --url or TARGET_URL environment variable.")
        sys.exit(2)

    if not args.secret:
        print("ERROR: Worker HMAC secret must be specified via --secret or WORKER_SHARED_SECRET environment variable.")
        sys.exit(2)

    base_url = args.url.rstrip("/")
    secret = args.secret

    print("=" * 80)
    print("PDFNest Studio V2 — Live Deployment Smoke Test (Remote HTTP)")
    print(f"Classification: LIVE REMOTE/CONTAINER SMOKE TEST")
    print(f"Target URL:     {base_url}")
    print(f"Expected Mode:  {'DISABLED (Rollback)' if args.expect_disabled else 'ENABLED (Persistent Pool)'}")
    if args.expected_workers:
        print(f"Expected Size:  {args.expected_workers} workers")
    print("=" * 80)

    client = httpx.Client(base_url=base_url, timeout=15.0)

    # 1. Health & Liveness
    print("\n1. Checking service liveness (/health)...")
    try:
        r = client.get("/health")
        if r.status_code != 200 or r.json().get("status") != "ok":
            print(f"   [FAIL] /health returned HTTP {r.status_code}: {r.text}")
            sys.exit(1)
        print(f"   [PASS] /health responded HTTP 200: {r.json()}")
    except Exception as exc:
        print(f"   [FAIL] Connection error to {base_url}: {exc}")
        sys.exit(1)

    # 2. Readiness Check
    print("\n2. Checking service readiness (/health/ready)...")
    try:
        r_ready = client.get("/health/ready")
        if r_ready.status_code == 200:
            print(f"   [PASS] /health/ready responded HTTP 200: {r_ready.json()}")
        else:
            print(f"   [WARN] /health/ready returned HTTP {r_ready.status_code}: {r_ready.text}")
    except Exception as exc:
        print(f"   [WARN] /health/ready request error: {exc}")

    # 3. Authentication Enforcement
    print("\n3. Testing HMAC Authentication Enforcement...")
    sample_pdf = make_sample_pdf("Unauthenticated Security Test")
    unauth_resp = client.post(
        "/api/v1/render/page",
        files={"file": ("unauth.pdf", sample_pdf, "application/pdf")},
        data={"page": "1", "dpi": "144"},
    )
    if unauth_resp.status_code == 401:
        print("   [PASS] Unauthorized request rejected with HTTP 401 Unauthorized.")
    else:
        print(f"   [FAIL] Unauthorized request returned HTTP {unauth_resp.status_code} (Expected 401)")
        sys.exit(1)

    # 4. Metrics Telemetry & Pool State
    print("\n4. Checking Metrics Telemetry (/api/v1/render/metrics)...")
    m_headers = generate_auth_headers(secret, "GET", "/api/v1/render/metrics")
    r_m = client.get("/api/v1/render/metrics", headers=m_headers)
    if r_m.status_code != 200:
        print(f"   [FAIL] /api/v1/render/metrics returned HTTP {r_m.status_code}: {r_m.text}")
        sys.exit(1)

    metrics = r_m.json()
    pp = metrics.get("persistent_pool", {})
    enabled = pp.get("enabled", False)
    configured = pp.get("configured_workers", 0)
    healthy = pp.get("healthy_workers", 0)
    available = pp.get("available_workers", 0)
    degraded = pp.get("degraded", False)

    print(f"   Pool Status: Enabled={enabled}, Configured={configured}, Healthy={healthy}, Available={available}, Degraded={degraded}")

    if args.expect_disabled:
        if enabled:
            print(f"   [FAIL] Expected pool to be disabled, but metrics show enabled={enabled}")
            sys.exit(1)
        print("   [PASS] Persistent pool is correctly disabled in metrics.")
    else:
        if not enabled:
            print(f"   [FAIL] Persistent pool is disabled in metrics (expected enabled=True)")
            sys.exit(1)
        if args.expected_workers and configured != args.expected_workers:
            print(f"   [FAIL] Expected {args.expected_workers} configured workers, found {configured}")
            sys.exit(1)
        if healthy != configured:
            print(f"   [FAIL] Healthy workers ({healthy}) != configured workers ({configured})")
            sys.exit(1)
        if degraded:
            print("   [FAIL] Persistent pool reports degraded=True")
            sys.exit(1)
        print("   [PASS] Persistent pool is fully healthy and ready.")

    # 5. Authenticated Page Rendering
    print("\n5. Executing Authenticated Page Render (/api/v1/render/page)...")
    render_pdf = make_sample_pdf("Live Render Test")
    r_headers = generate_auth_headers(secret, "POST", "/api/v1/render/page")
    render_resp = client.post(
        "/api/v1/render/page",
        headers=r_headers,
        files={"file": ("live_test.pdf", render_pdf, "application/pdf")},
        data={"page": "1", "dpi": "144"},
    )
    if render_resp.status_code != 200:
        print(f"   [FAIL] Render request failed with HTTP {render_resp.status_code}: {render_resp.text}")
        sys.exit(1)

    if render_resp.headers.get("content-type") != "image/jpeg":
        print(f"   [FAIL] Invalid Content-Type: {render_resp.headers.get('content-type')} (Expected image/jpeg)")
        sys.exit(1)

    img = Image.open(io.BytesIO(render_resp.content))
    print(f"   [PASS] Render succeeded: Valid {img.format} image, dimensions {img.width}x{img.height}, {len(render_resp.content)} bytes")
    print(f"          Telemetry: Queue Wait: {render_resp.headers.get('x-queue-wait-ms')} ms | Exec: {render_resp.headers.get('x-render-exec-ms')} ms")

    # 6. Production Security Simulation Gating
    print("\n6. Testing Production Simulation Header Immunity...")
    sim_headers = generate_auth_headers(secret, "POST", "/api/v1/render/page")
    sim_headers["X-Test-Simulate-Crash"] = "true"
    sim_headers["X-Test-Simulate-Hang"] = "true"

    sim_resp = client.post(
        "/api/v1/render/page",
        headers=sim_headers,
        files={"file": ("sim_test.pdf", render_pdf, "application/pdf")},
        data={"page": "1", "dpi": "144"},
    )
    if sim_resp.status_code != 200:
        print(f"   [FAIL] Simulation header request failed with HTTP {sim_resp.status_code}")
        sys.exit(1)
    sim_img = Image.open(io.BytesIO(sim_resp.content))
    assert sim_img.format == "JPEG"
    print("   [PASS] Simulation headers safely ignored in production mode (returned valid JPEG without worker disruption).")

    print("\n" + "=" * 80)
    print("ALL LIVE DEPLOYMENT SMOKE TESTS PASSED SUCCESSFULLY.")
    print("=" * 80)


if __name__ == "__main__":
    main()

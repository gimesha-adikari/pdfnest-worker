from __future__ import annotations

import io
import json
import os
import resource
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Add worker app root to sys.path
APP_ROOT = Path(__file__).resolve().parents[4]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Warm up Python interpreter & import PyMuPDF + Pillow once
t_init_start = time.perf_counter()
t_init_cpu_start = time.process_time()

from app.api.tools.render.renderer import render_pdf_page_to_jpeg
import pymupdf as fitz
from PIL import Image

init_wall_ms = (time.perf_counter() - t_init_start) * 1000.0
init_cpu_ms = (time.process_time() - t_init_cpu_start) * 1000.0


def read_exact(stream: io.BufferedReader, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            raise EOFError("Stream closed while reading exact bytes")
        data.extend(chunk)
    return bytes(data)


def get_schedstat_wait_ns() -> int:
    """Reads raw scheduling runqueue delay in nanoseconds from /proc/self/schedstat (Field 1)."""
    try:
        with open("/proc/self/schedstat", "r", encoding="utf-8") as f:
            parts = f.read().strip().split()
            if len(parts) >= 2:
                return int(parts[1])
    except Exception:
        pass
    return 0


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    # Send ready handshake with initialization telemetry
    handshake = {
        "status": "ready",
        "pid": os.getpid(),
        "init_wall_ms": round(init_wall_ms, 2),
        "init_cpu_ms": round(init_cpu_ms, 2),
    }
    handshake_bytes = json.dumps(handshake).encode("utf-8")
    stdout.write(struct.pack(">I", len(handshake_bytes)) + handshake_bytes)
    stdout.flush()

    while True:
        try:
            # Read 4-byte header length
            header_len_bytes = stdin.read(4)
            if not header_len_bytes or len(header_len_bytes) < 4:
                break

            header_len = struct.unpack(">I", header_len_bytes)[0]
            if header_len > 64 * 1024:
                sys.stderr.write(f"[Worker {os.getpid()}] Header length exceeds 64KB: {header_len}\n")
                break

            header_bytes = read_exact(stdin, header_len)
            header = json.loads(header_bytes.decode("utf-8"))

            action = header.get("action", "render")
            if action == "shutdown":
                break

            # Read and validate PDF payload length
            payload_len = header.get("payload_len", 0)
            if isinstance(payload_len, bool) or not isinstance(payload_len, int) or payload_len < 0 or payload_len > 100 * 1024 * 1024:
                sys.stderr.write(f"[Worker {os.getpid()}] Invalid payload length: {payload_len}\n")
                break

            pdf_bytes = read_exact(stdin, payload_len) if payload_len > 0 else b""

            # Failure injection check (defense-in-depth: gated strictly by flag AND non-production environment)
            failure_injection_enabled = (
                os.getenv("ENABLE_RENDER_FAILURE_INJECTION", "false").lower() in ("true", "1", "yes", "on")
                and os.getenv("APP_ENV", "development").lower() in ("development", "test", "testing")
            )
            if failure_injection_enabled:
                if header.get("simulate_crash"):
                    os._exit(139) # SIGSEGV
                if header.get("simulate_hang"):
                    time.sleep(60.0)

            # Schedstat delta and CPU measurement
            sched_before_ns = get_schedstat_wait_ns()
            t0_wall = time.perf_counter()
            t0_cpu = time.process_time()
            ru_before = resource.getrusage(resource.RUSAGE_SELF)

            status = "ok"
            error_type = ""
            err_msg = ""
            jpeg_bytes = b""

            # Strict Type Validation (No boolean or invalid string coercion)
            page_val = header.get("page", 1)
            dpi_val = header.get("dpi", 144.0)
            clip_val = header.get("clip")

            # Validate page
            if isinstance(page_val, bool) or not isinstance(page_val, int) or page_val < 1:
                status = "error"
                error_type = "USER_INPUT_ERROR"
                err_msg = f"Page number must be an integer >= 1 (received: {type(page_val).__name__} {page_val!r})"
            # Validate dpi
            elif isinstance(dpi_val, bool) or not isinstance(dpi_val, (int, float)) or dpi_val <= 0:
                status = "error"
                error_type = "USER_INPUT_ERROR"
                err_msg = f"DPI must be a positive number (received: {type(dpi_val).__name__} {dpi_val!r})"

            # Validate clip
            clip_x0, clip_y0, clip_x1, clip_y1 = None, None, None, None
            if status != "error" and clip_val is not None:
                if isinstance(clip_val, (list, tuple)) and len(clip_val) == 4 and not any(isinstance(c, bool) for c in clip_val) and all(isinstance(c, (int, float)) for c in clip_val):
                    clip_x0 = float(clip_val[0])
                    clip_y0 = float(clip_val[1])
                    clip_x1 = float(clip_val[2])
                    clip_y1 = float(clip_val[3])
                else:
                    status = "error"
                    error_type = "USER_INPUT_ERROR"
                    err_msg = f"Clip must be an array of 4 numeric coordinates [x0, y0, x1, y1] (received: {clip_val!r})"

            # Validate PDF bytes
            if status != "error":
                if not pdf_bytes:
                    status = "error"
                    error_type = "USER_INPUT_ERROR"
                    err_msg = "Empty file uploaded"
                elif not pdf_bytes.startswith(b"%PDF"):
                    status = "error"
                    error_type = "USER_INPUT_ERROR"
                    err_msg = "File is not a valid PDF"
                else:
                    try:
                        jpeg_bytes = render_pdf_page_to_jpeg(
                            pdf_bytes=pdf_bytes,
                            page_number=page_val,
                            dpi=float(dpi_val),
                            clip_x0=clip_x0,
                            clip_y0=clip_y0,
                            clip_x1=clip_x1,
                            clip_y1=clip_y1,
                        )
                    except ValueError as val_exc:
                        status = "error"
                        error_type = "USER_INPUT_ERROR"
                        err_msg = str(val_exc)
                    except Exception as exc:
                        status = "error"
                        error_type = "RENDER_ERROR"
                        err_msg = str(exc)

            t1_wall = time.perf_counter()
            t1_cpu = time.process_time()
            ru_after = resource.getrusage(resource.RUSAGE_SELF)
            sched_after_ns = get_schedstat_wait_ns()

            render_wall_ms = (t1_wall - t0_wall) * 1000.0
            render_cpu_ms = (t1_cpu - t0_cpu) * 1000.0
            user_cpu_ms = (ru_after.ru_utime - ru_before.ru_utime) * 1000.0
            sys_cpu_ms = (ru_after.ru_stime - ru_before.ru_stime) * 1000.0
            vol_ctx = ru_after.ru_nvcsw - ru_before.ru_nvcsw
            invol_ctx = ru_after.ru_nivcsw - ru_before.ru_nivcsw

            # True per-request delta for kernel runqueue wait in ms
            req_runqueue_wait_ms = 0.0
            if sched_before_ns > 0 and sched_after_ns >= sched_before_ns:
                req_runqueue_wait_ms = round((sched_after_ns - sched_before_ns) / 1_000_000.0, 2)

            resp_header = {
                "request_id": header.get("request_id"),
                "status": status,
                "error_type": error_type,
                "error_message": err_msg,
                "render_wall_ms": round(render_wall_ms, 2),
                "render_cpu_ms": round(render_cpu_ms, 2),
                "user_cpu_ms": round(user_cpu_ms, 2),
                "sys_cpu_ms": round(sys_cpu_ms, 2),
                "vol_ctx": vol_ctx,
                "invol_ctx": invol_ctx,
                "runqueue_wait_ms": req_runqueue_wait_ms,
                "max_rss_kb": ru_after.ru_maxrss, # Kernel process high-water mark RSS in KB (Linux)
                "pid": os.getpid(),
                "payload_len": len(jpeg_bytes),
            }

            resp_header_bytes = json.dumps(resp_header).encode("utf-8")
            stdout.write(struct.pack(">I", len(resp_header_bytes)) + resp_header_bytes + jpeg_bytes)
            stdout.flush()

        except EOFError:
            break
        except Exception as exc:
            sys.stderr.write(f"[Worker {os.getpid()}] Protocol loop error: {exc}\n")
            sys.stderr.flush()
            break


if __name__ == "__main__":
    main()

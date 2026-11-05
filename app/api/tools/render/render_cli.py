from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .child_limits import MAX_RENDER_CHILD_FILE_BYTES, apply_render_child_limits


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated PyMuPDF render CLI")
    parser.add_argument("--input", required=True, help="Path to input PDF file")
    parser.add_argument("--output", required=True, help="Path to output JPEG file")
    parser.add_argument("--page", type=int, required=True, help="Page number (1-based)")
    parser.add_argument("--dpi", type=float, default=144.0, help="Rendering DPI")
    parser.add_argument("--clip-x0", type=float, default=None)
    parser.add_argument("--clip-y0", type=float, default=None)
    parser.add_argument("--clip-x1", type=float, default=None)
    parser.add_argument("--clip-y1", type=float, default=None)
    parser.add_argument("--owner-pid", type=int, default=None)

    args = parser.parse_args()

    if args.owner_pid is not None:
        import os
        from .supervisor import supervise, ROOT_LOCK_FD_ENV, CANCEL_FD_ENV, DRAIN_FD_ENV
        from .limiter import HARD_MAX_RENDER_TIMEOUT_SECONDS
        child_args = sys.argv[1:]
        owner_index = child_args.index("--owner-pid")
        del child_args[owner_index:owner_index + 2]
        sys.exit(supervise(
            [sys.executable, "-m", "app.api.tools.render.render_cli", *child_args],
            owner_pid=args.owner_pid,
            root_lock_fd=int(os.environ[ROOT_LOCK_FD_ENV]),
            cancel_fd=int(os.environ[CANCEL_FD_ENV]),
            drain_fd=int(os.environ[DRAIN_FD_ENV]),
            wall_timeout=HARD_MAX_RENDER_TIMEOUT_SECONDS,
        ))

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.stderr.write(f"Input file not found: {args.input}\n")
        sys.exit(1)

    try:
        apply_render_child_limits()
        from .renderer import render_pdf_page_to_jpeg
        with input_path.open("rb") as source:
            pdf_bytes = source.read(MAX_RENDER_CHILD_FILE_BYTES + 1)
        if len(pdf_bytes) > MAX_RENDER_CHILD_FILE_BYTES:
            raise ValueError("PDF exceeds the preview input-size limit")
        jpeg_bytes = render_pdf_page_to_jpeg(
            pdf_bytes=pdf_bytes,
            page_number=args.page,
            dpi=args.dpi,
            clip_x0=args.clip_x0,
            clip_y0=args.clip_y0,
            clip_x1=args.clip_x1,
            clip_y1=args.clip_y1,
        )
        if len(jpeg_bytes) > MAX_RENDER_CHILD_FILE_BYTES:
            raise ValueError("Rendered output exceeds the child file-size limit")
        output_path.write_bytes(jpeg_bytes)
        
        # Telemetry: output kernel rusage & schedstat of child render process to stdout
        import resource
        import os
        import json
        ru = resource.getrusage(resource.RUSAGE_SELF)
        
        runqueue_wait_ms = 0.0
        try:
            with open("/proc/self/schedstat", "r") as f_sched:
                parts = f_sched.read().strip().split()
                if len(parts) >= 2:
                    # parts[1] is runqueue wait time in nanoseconds
                    runqueue_wait_ms = round(int(parts[1]) / 1_000_000.0, 2)
        except Exception:
            pass

        sys.stdout.write(json.dumps({
            "pid": os.getpid(),
            "user_cpu_ms": round(ru.ru_utime * 1000.0, 2),
            "sys_cpu_ms": round(ru.ru_stime * 1000.0, 2),
            "total_cpu_ms": round((ru.ru_utime + ru.ru_stime) * 1000.0, 2),
            "max_rss_kb": ru.ru_maxrss,
            "vol_ctx": ru.ru_nvcsw,
            "invol_ctx": ru.ru_nivcsw,
            "runqueue_wait_ms": runqueue_wait_ms,
        }) + "\n")
        sys.stdout.flush()
    except ValueError as exc:
        sys.stderr.write(f"Render error: {exc}\n")
        sys.exit(3)
    except Exception as exc:
        sys.stderr.write(f"Render error: {exc}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()


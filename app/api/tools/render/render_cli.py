from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .renderer import render_pdf_page_to_jpeg


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

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.stderr.write(f"Input file not found: {args.input}\n")
        sys.exit(1)

    pdf_bytes = input_path.read_bytes()

    try:
        jpeg_bytes = render_pdf_page_to_jpeg(
            pdf_bytes=pdf_bytes,
            page_number=args.page,
            dpi=args.dpi,
            clip_x0=args.clip_x0,
            clip_y0=args.clip_y0,
            clip_x1=args.clip_x1,
            clip_y1=args.clip_y1,
        )
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
    except Exception as exc:
        sys.stderr.write(f"Render error: {exc}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()


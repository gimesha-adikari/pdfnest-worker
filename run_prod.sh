#!/usr/bin/env bash

set -euo pipefail

cleanup() {
    echo
    echo "Stopping PDFNest Worker..."

    kill "$UVICORN_PID" 2>/dev/null || true
    kill "$DRAMATIQ_PID" 2>/dev/null || true

    wait "$UVICORN_PID" 2>/dev/null || true
    wait "$DRAMATIQ_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

export APP_ENV=production

CPU_COUNT=$(( $(nproc) - 1 ))

if [ "$CPU_COUNT" -lt 1 ]; then
    CPU_COUNT=1
fi

echo "======================================"
echo " PDFNest Worker"
echo "======================================"
echo "CPU Cores : $CPU_COUNT"
echo

uv run uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 &
UVICORN_PID=$!

sleep 2

uv run dramatiq app.jobs.actors \
    --processes "$CPU_COUNT" \
    --threads 1 &
DRAMATIQ_PID=$!

echo
echo "FastAPI running"
echo "Dramatiq running"
echo

wait -n
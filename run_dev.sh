#!/usr/bin/env bash

set -euo pipefail

cleanup() {
    echo
    echo "Stopping services..."

    kill "$UVICORN_PID" 2>/dev/null || true
    kill "$DRAMATIQ_PID" 2>/dev/null || true

    wait "$UVICORN_PID" 2>/dev/null || true
    wait "$DRAMATIQ_PID" 2>/dev/null || true

    echo "Stopped."
}

trap cleanup EXIT INT TERM

echo "Starting FastAPI..."
uv run uvicorn app.main:app --reload &
UVICORN_PID=$!

sleep 2

echo "Starting Dramatiq..."
uv run dramatiq app.jobs.actors \
    --processes "$(nproc)" \
    --threads 1 &
DRAMATIQ_PID=$!

echo
echo "FastAPI   PID : $UVICORN_PID"
echo "Dramatiq  PID : $DRAMATIQ_PID"
echo "Workers       : $(nproc)"
echo

wait -n
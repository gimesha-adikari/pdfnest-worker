#!/usr/bin/env bash

set -euo pipefail

export APP_VERSION=0.1.0
export APP_ENV=development
export ALLOWED_ORIGINS=http://localhost:8080
export HOST=0.0.0.0
export PORT=8000
export REDIS_URL=redis://localhost:6379/0
export FRONTEND_URL=http://localhost:3000
export BACKEND_URL=http://localhost:8080

# Remote-storage settings must be supplied by the shell or an ignored local
# environment file; credentials and provider endpoints do not belong in this
# tracked example launcher.
export R2_BUCKET="${R2_BUCKET:-pdfnest-storage}"

uv run uvicorn app.main:app --reload &
UVICORN_PID=$!

sleep 2

uv run dramatiq app.jobs.actors \
    --processes "$(nproc)" \
    --threads 1 &
DRAMATIQ_PID=$!

wait -n

#!/usr/bin/env bash

pkill -f "uvicorn app.main:app" || true
pkill -f "dramatiq app.jobs.actors" || true

echo "Stopped."
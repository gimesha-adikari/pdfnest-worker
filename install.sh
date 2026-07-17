#!/usr/bin/env bash

set -euo pipefail

echo "======================================"
echo " Installing PDFNest Worker"
echo "======================================"

sudo apt update

sudo apt install -y \
    build-essential \
    curl \
    git \
    libreoffice \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-draw \
    libreoffice-java-common \
    fonts-dejavu \
    fonts-liberation \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    ghostscript \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    ffmpeg

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$HOME/.local/bin:$PATH"

uv sync

echo
echo "======================================"
echo " Installation Complete"
echo "======================================"
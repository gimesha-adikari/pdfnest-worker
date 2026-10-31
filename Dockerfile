# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

RUN --mount=type=cache,id=platen-bookworm-lists,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=platen-bookworm-archives,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update && \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    tesseract-ocr \
    ghostscript \
    poppler-utils \
    libreoffice \
    chromium \
    libjpeg62-turbo \
    libopenjp2-7 \
    zlib1g \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-cjk \
    libglib2.0-0 \
    libnss3 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libfontconfig1

WORKDIR /app

RUN --mount=type=cache,id=platen-pip,target=/root/.cache/pip \
    pip install --timeout 30 --retries 3 uv==0.11.28

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=platen-uv,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --frozen --no-dev --no-install-project && \
    chown -R 10001:10001 /opt/venv

COPY --chown=10001:10001 . .

COPY --chown=10001:10001 tesseract/tessdata/ ${TESSDATA_PREFIX}/

RUN useradd --system --uid 10001 --create-home appuser && \
    chown appuser:appuser /app

USER appuser

ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health/ready" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
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
    libglib2.0-0 \
    libnss3 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --system --uid 10001 --create-home appuser && \
    chown -R appuser:appuser /app /opt/venv

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "python - <<'PY'\nimport redis\nprint(redis.__file__)\nprint(redis.__version__)\nimport pathlib\np=pathlib.Path(redis.__file__).parent/'client.py'\nprint(p)\nprint(p.read_text()[:400])\nPY"]

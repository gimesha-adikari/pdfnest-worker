![Project cover](cover.png)
# Platen PDF Worker

## Overview

Platen PDF Worker is the FastAPI processing service behind the Platen PDF platform. It handles the heavier PDF operations delegated by the Platen PDF Backend, including analysis, metadata read/write, redaction, signing, editor extraction/compile flows, and markup jobs.

## Features

- **Analysis**: Extract page structure and text/image statistics.
- **Metadata**: Read and update PDF metadata fields.
- **Redaction**: Securely black out sensitive text.
- **Signing**: Apply signature images to PDFs.
- **Editor**: Extract and compile editor layouts.
- **Markup**: Highlight, underline, and strikeout selections with asynchronous job handling.
- **Conversion**: Image → PDF, PDF → image, OCR to searchable PDF.

## Tech Stack

- Python 3.12
- FastAPI
- Uvicorn
- Dramatiq + Redis (background job queue)
- PyMuPDF, pdfplumber, pdf2docx, python-pptx, pandas, camelot, Pillow, pytesseract, psutil

## Prerequisites

```bash
python --version

tesseract --version
```

On Ubuntu/Debian the included install script sets up required runtime packages:

```bash
sudo apt update
sudo apt install ghostscript tesseract-ocr libreoffice ffmpeg poppler-utils
```

## Getting Started

```bash
# Install dependencies
uv sync

# Run in development
bash run_dev.sh
# or manually
uv run uvicorn app.main:app --reload
uv run dramatiq app.jobs.actors
```

The worker exposes health endpoints at `/health`, `/health/live`, and `/health/ready`.

## Environment Variables

- `APP_VERSION`
- `APP_ENV`
- `HOST`
- `PORT`
- `REDIS_URL`
- `ALLOWED_ORIGINS`
- `OCR_TEXT_ENGINE` – OCR Text V2 execution selector (`internal` by default;
  `sdk` is an explicit opt-in when the independently installed
  `platen-document` package is available).
- `SEARCHABLE_PDF_ENGINE` – Searchable PDF V2 execution selector (`internal`
  by default; `sdk` is an explicit opt-in when the independently installed
  `platen-document` package is available).
- `DOCUMENT_EXTRACTION_ENGINE` – Document Extraction V2 execution selector
  (`internal` by default; `sdk` is an explicit opt-in when the independently
  installed `platen-document` package is available).
- `PDF_TO_MARKDOWN_ENGINE` – PDF-to-Markdown V2 execution selector (`internal`
  by default; `sdk` is an explicit opt-in when the independently installed
  `platen-document` package is available).
- `PDF_TO_WORD_OCR_ENGINE` – PDF-to-Word OCR/scanned fallback selector
  (`internal` by default; `sdk` is an explicit opt-in when the independently
  installed `platen-document` package is available). Trusted native PDF-to-
  Word conversion remains on the existing `pdf2docx` path.
- `OCR_MARKUP_ENGINE` – OCR-aware Highlight/Underline/Strikeout V2 execution
  selector (`internal` by default; `sdk` is an explicit opt-in when the
  independently installed `platen-document` package is available).
- `EDITOR_OCR_ENGINE` – General Editor OCR V2 execution selector (`internal`
  by default; `sdk` is an explicit opt-in when the independently installed
  `platen-document` package is available). Studio editor extraction remains on
  the internal path in this milestone.

- LEGACY_EDITOR_OCR_ENGINE=internal|sdk controls the ordinary /edit-pdf OCR
  and document extraction fallback only. It is independent from
  EDITOR_OCR_ENGINE and defaults to internal.
- LEGACY_MARKUP_OCR_ENGINE=internal|sdk controls the ordinary legacy
  /highlight-pdf, /underline-pdf, and /strikeout-pdf OCR markup fallback only.
  It is independent from OCR_MARKUP_ENGINE and all Editor selectors, defaults
  to internal, and does not control Studio markup.

## Project Structure

```text
app/
├── main.py               # FastAPI entry point
├── core/                 # Core utilities and config
├── jobs/                 # Dramatiq job definitions
└── api/tools/            # Tool routers (analyzer, editor, markup, etc.)
```

## API Overview

All tool endpoints accept `multipart/form-data`.

### Health

- `GET /health`
- `GET /health/live`
- `GET /health/ready`

### Analyzer

- `POST /api/v1/analyzer/analyze` – Analyze PDF structure and statistics.

### Metadata

- `POST /api/v1/metadata/read`
- `POST /api/v1/metadata/write`

### Redact

- `POST /api/v1/redact`

### Sign

- `POST /api/v1/sign`

### Editor

- `POST /api/v1/editor/extract`
- `POST /api/v1/editor/compile`
- `GET /api/v1/editor/jobs/:job_id`
- `GET /api/v1/editor/jobs/:job_id/download`

### Markup

- `POST /api/v1/markup/highlight`
- `POST /api/v1/markup/underline`
- `POST /api/v1/markup/strikeout`
- `GET /api/v1/markup/jobs/:job_id`
- `GET /api/v1/markup/jobs/:job_id/download`

## Notes

- The worker is intended to be called by the Platen PDF Backend, not directly by the frontend.
- Temporary files are cleaned up after each request.
- Some workflows are synchronous; others use Redis‑backed jobs via Dramatiq.

## License

This project is licensed under the terms in [LICENSE](./LICENSE).

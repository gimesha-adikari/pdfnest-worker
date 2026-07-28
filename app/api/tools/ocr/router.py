from __future__ import annotations

import logging
import os
import shutil
import tempfile
import traceback
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .document import (
    build_searchable_pdf_from_images,
    extract_text_from_pdf,
    get_installed_tesseract_languages,
    normalize_lang_spec,
    safe_suffix,
)
from .languages import language_name

router = APIRouter(prefix="/api/v1/ocr", tags=["ocr"])
logger = logging.getLogger(__name__)

def _cleanup_paths(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _parse_language_list() -> list[dict[str, str]]:
    default_lang = normalize_lang_spec(os.getenv("OCR_DEFAULT_LANG", "eng")).strip() or "eng"
    default_lang = default_lang.split("+", 1)[0]

    installed = sorted(get_installed_tesseract_languages())

    languages = [
        {
            "code": code,
            "name": language_name(code),
        }
        for code in installed
    ]

    if default_lang not in installed:
        languages.insert(
            0,
            {
                "code": default_lang,
                "name": language_name(default_lang),
            },
        )

    return languages


@router.get("/languages")
async def get_languages():
    default_lang = normalize_lang_spec(os.getenv("OCR_DEFAULT_LANG", "eng")).strip() or "eng"
    default_lang = default_lang.split("+", 1)[0]
    return {
        "default": default_lang,
        "languages": _parse_language_list(),
    }


@router.post("/extract-text")
async def extract_text_from_pdf_route(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        file_password: str | None = Form(None),
        lang: str = Form("eng"),
):
    input_fd, input_path = tempfile.mkstemp(
        prefix="pdfnest-ocr-in-",
        suffix=".pdf",
    )
    os.close(input_fd)

    output_fd, output_path = tempfile.mkstemp(
        prefix="pdfnest-ocr-out-",
        suffix=".txt",
    )
    os.close(output_fd)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        lang = normalize_lang_spec(lang)

        await run_in_threadpool(
            extract_text_from_pdf,
            input_path,
            output_path,
            lang,
            file_password,
        )

        background_tasks.add_task(os.remove, output_path)

        download_name = f"{Path(file.filename or 'document').stem}_ocr.txt"
        return FileResponse(
            output_path,
            filename=download_name,
            media_type="text/plain",
            background=background_tasks,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("OCR failed")
        raise
    finally:
        _cleanup_paths(input_path)


@router.post("/to-text-pdf")
async def images_to_searchable_pdf_route(
        background_tasks: BackgroundTasks,
        images: list[UploadFile] = File(...),
        lang: str = Form("eng"),
):
    if not images:
        raise HTTPException(status_code=400, detail="No images provided")

    temp_paths: list[str] = []

    output_fd, output_path = tempfile.mkstemp(
        prefix="pdfnest-ocr-out-",
        suffix=".pdf",
    )
    os.close(output_fd)

    try:
        lang = normalize_lang_spec(lang)

        for upload in images:
            fd, tmp_path = tempfile.mkstemp(
                prefix="pdfnest-ocr-img-",
                suffix=safe_suffix(upload.filename, ".img"),
            )
            os.close(fd)

            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(upload.file, f)

            temp_paths.append(tmp_path)

        await run_in_threadpool(
            build_searchable_pdf_from_images,
            temp_paths,
            output_path,
            lang,
        )

        background_tasks.add_task(os.remove, output_path)

        return FileResponse(
            output_path,
            filename="ocr_searchable.pdf",
            media_type="application/pdf",
            background=background_tasks,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Searchable PDF generation failed")
        raise
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
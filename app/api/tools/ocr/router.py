import asyncio
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.jobs.cancellation import JobCancelledException
from .document import (
    R2ImageRef,
    build_searchable_pdf_from_images,
    build_searchable_pdf_from_r2_images,
    extract_text_from_pdf,
    get_installed_tesseract_languages,
    normalize_lang_spec,
    safe_suffix,
)
from .languages import language_name

router = APIRouter(prefix="/api/v1/ocr", tags=["ocr"])
logger = logging.getLogger(__name__)


def create_route_cancellation_checker(request: Request) -> tuple[Callable[[], None], Callable[[], None]]:
    cancel_event = threading.Event()
    loop = asyncio.get_running_loop()

    async def _monitor():
        while not cancel_event.is_set():
            try:
                if await request.is_disconnected():
                    logger.info("[OCR CANCELLATION] Client disconnect detected on route %s", request.url.path)
                    cancel_event.set()
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)

    task = loop.create_task(_monitor())

    def check():
        if cancel_event.is_set():
            logger.info("[OCR CANCELLATION] Triggering cancellation check for disconnected client")
            raise JobCancelledException("Request cancelled by client disconnect")

    def cleanup():
        cancel_event.set()
        task.cancel()

    return check, cleanup


class OCRR2Image(BaseModel):
    key: str = Field(..., min_length=1)
    name: str = ""
    type: str = ""
    size: int = 0


class OCRR2JobRequest(BaseModel):
    tool: str = "image_to_text_pdf"
    lang: str = "eng"
    sessionId: str | None = None
    files: list[OCRR2Image] = Field(default_factory=list)


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
        request: Request,
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        file_password: str | None = Form(None),
        lang: str = Form("eng"),
):
    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)

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
            cancellation_check=check_cancel,
        )

        with open(output_path, "rb") as out_f:
            content = out_f.read()

        if os.path.exists(output_path):
            os.remove(output_path)

        download_name = f"{Path(file.filename or 'document').stem}_ocr.txt"
        return Response(
            content=content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    except JobCancelledException as exc:
        logger.info("[OCR CANCELLATION] extract-text route request cancelled: %s", exc)
        raise HTTPException(status_code=499, detail="Client Disconnected")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("OCR failed")
        raise
    finally:
        cleanup_cancel()
        _cleanup_paths(input_path, output_path)


@router.post("/to-text-pdf")
async def images_to_searchable_pdf_route(
        request: Request,
        background_tasks: BackgroundTasks,
        images: list[UploadFile] = File(...),
        lang: str = Form("eng"),
):
    if not images:
        raise HTTPException(status_code=400, detail="No images provided")

    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)

    temp_paths: list[str] = []

    output_fd, output_path = tempfile.mkstemp(
        prefix="pdfnest-ocr-out-",
        suffix=".pdf",
    )
    os.close(output_fd)

    success = False
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
            cancellation_check=check_cancel,
        )

        background_tasks.add_task(os.remove, output_path)

        res = FileResponse(
            output_path,
            filename="ocr_searchable.pdf",
            media_type="application/pdf",
            background=background_tasks,
        )
        success = True
        return res

    except JobCancelledException as exc:
        logger.info("[OCR CANCELLATION] to-text-pdf route request cancelled: %s", exc)
        raise HTTPException(status_code=499, detail="Client Disconnected")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Searchable PDF generation failed")
        raise
    finally:
        cleanup_cancel()
        if not success:
            _cleanup_paths(output_path)
        for path in temp_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


@router.post("/to-text-pdf-r2")
async def images_to_searchable_pdf_from_r2_route(
        request: Request,
        background_tasks: BackgroundTasks,
        payload: OCRR2JobRequest,
):
    if not payload.files:
        raise HTTPException(status_code=400, detail="No images provided")

    check_cancel, cleanup_cancel = create_route_cancellation_checker(request)

    output_fd, output_path = tempfile.mkstemp(
        prefix="pdfnest-ocr-out-",
        suffix=".pdf",
    )
    os.close(output_fd)

    success = False
    try:
        lang = normalize_lang_spec(payload.lang)

        refs = [
            R2ImageRef(
                key=item.key,
                name=item.name,
                content_type=item.type,
                size=item.size,
            )
            for item in payload.files
        ]

        await run_in_threadpool(
            build_searchable_pdf_from_r2_images,
            refs,
            output_path,
            lang,
            cancellation_check=check_cancel,
        )

        background_tasks.add_task(os.remove, output_path)

        res = FileResponse(
            output_path,
            filename="ocr_searchable.pdf",
            media_type="application/pdf",
            background=background_tasks,
        )
        success = True
        return res

    except JobCancelledException as exc:
        logger.info("[OCR CANCELLATION] to-text-pdf-r2 route request cancelled: %s", exc)
        raise HTTPException(status_code=499, detail="Client Disconnected")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Searchable PDF generation from R2 failed")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        cleanup_cancel()
        if not success:
            _cleanup_paths(output_path)
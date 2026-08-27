from __future__ import annotations

import os
import shutil
import tempfile
from fastapi import BackgroundTasks

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse

from .document import (
    preserve_metadata,
    read_metadata,
    write_metadata,
)
from .models import MetadataResponse

router = APIRouter(
    prefix="/api/v1/metadata",
    tags=["metadata"],
)


@router.post(
    "/read",
    response_model=MetadataResponse,
)
async def read_pdf_metadata(
        file: UploadFile = File(...),
        file_password: str | None = Form(None),
):
    fd, input_path = tempfile.mkstemp(
        prefix="pdfnest-meta-",
        suffix=".pdf",
    )
    os.close(fd)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        return read_metadata(
            input_path=input_path,
            password=file_password,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

    finally:
        try:
            os.remove(input_path)
        except FileNotFoundError:
            pass


@router.post("/write")
async def write_pdf_metadata(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        title: str = Form(""),
        author: str = Form(""),
        subject: str = Form(""),
        keywords: str = Form(""),
        file_password: str | None = Form(None),
):
    fd1, input_path = tempfile.mkstemp(
        prefix="pdfnest-meta-in-",
        suffix=".pdf",
    )
    os.close(fd1)

    fd2, output_path = tempfile.mkstemp(
        prefix="pdfnest-meta-out-",
        suffix=".pdf",
    )
    os.close(fd2)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        write_metadata(
            input_path=input_path,
            output_path=output_path,
            title=title,
            author=author,
            subject=subject,
            keywords=keywords,
            password=file_password,
        )

        background_tasks.add_task(os.remove, output_path)

        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename="metadata.pdf",
            background=background_tasks,
        )

    except Exception as e:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

    finally:
        try:
            os.remove(input_path)
        except FileNotFoundError:
            pass


@router.post("/preserve")
async def preserve_pdf_metadata(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        metadata_source: UploadFile = File(...),
        file_password: str | None = Form(None),
        source_password: str | None = Form(None),
):
    fd1, input_path = tempfile.mkstemp(prefix="pdfnest-meta-preserve-in-", suffix=".pdf")
    os.close(fd1)
    fd2, source_path = tempfile.mkstemp(prefix="pdfnest-meta-preserve-source-", suffix=".pdf")
    os.close(fd2)
    fd3, output_path = tempfile.mkstemp(prefix="pdfnest-meta-preserve-out-", suffix=".pdf")
    os.close(fd3)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with open(source_path, "wb") as f:
            shutil.copyfileobj(metadata_source.file, f)

        preserve_metadata(
            input_path=input_path,
            metadata_source_path=source_path,
            output_path=output_path,
            password=file_password,
            source_password=source_password,
        )

        background_tasks.add_task(os.remove, output_path)
        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename="metadata-preserved.pdf",
            background=background_tasks,
        )
    except Exception as e:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for path in (input_path, source_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

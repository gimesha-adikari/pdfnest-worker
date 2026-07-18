from __future__ import annotations

import json
import os
import shutil
import tempfile

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse

from .document import redact_pdf
from .models import RedactBox

router = APIRouter(
    prefix="/api/v1/redact",
    tags=["redact"],
)


@router.post("")
async def redact_document(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        keywords: str = Form(""),
        boxes: str = Form("[]"),
):
    fd1, input_path = tempfile.mkstemp(
        prefix="pdfnest-redact-in-",
        suffix=".pdf",
    )
    os.close(fd1)

    fd2, output_path = tempfile.mkstemp(
        prefix="pdfnest-redact-out-",
        suffix=".pdf",
    )
    os.close(fd2)

    try:

        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        keyword_list = [
            x.strip()
            for x in keywords.split("|||")
            if x.strip()
        ]

        parsed_boxes = [
            RedactBox.model_validate(item)
            for item in json.loads(boxes)
        ]

        redact_pdf(
            input_path=input_path,
            output_path=output_path,
            keywords=keyword_list,
            boxes=parsed_boxes,
        )

        background_tasks.add_task(os.remove, output_path)

        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename="redacted.pdf",
            background=background_tasks,
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
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

from .document import sign_pdf
from .models import SignatureStamp

router = APIRouter(
    prefix="/api/v1/sign",
    tags=["sign"],
)


@router.post("")
async def sign_document(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        signature: UploadFile = File(...),
        stamps: str = Form(...),
):

    pdf_fd, pdf_path = tempfile.mkstemp(
        suffix=".pdf",
        prefix="pdfnest-sign-",
    )
    os.close(pdf_fd)

    sig_fd, signature_path = tempfile.mkstemp(
        suffix=".png",
        prefix="pdfnest-signature-",
    )
    os.close(sig_fd)

    out_fd, output_path = tempfile.mkstemp(
        suffix=".pdf",
        prefix="pdfnest-output-",
    )
    os.close(out_fd)

    try:

        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        with open(signature_path, "wb") as f:
            shutil.copyfileobj(signature.file, f)

        parsed = json.loads(stamps)

        stamp_models = [
            SignatureStamp.model_validate(item)
            for item in parsed
        ]

        sign_pdf(
            input_pdf=pdf_path,
            signature_image=signature_path,
            output_pdf=output_path,
            stamps=stamp_models,
        )

        background_tasks.add_task(os.remove, output_path)

        return FileResponse(
            output_path,
            filename="signed.pdf",
            media_type="application/pdf",
            background=background_tasks,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )

    finally:

        try:
            os.remove(pdf_path)
        except FileNotFoundError:
            pass

        try:
            os.remove(signature_path)
        except FileNotFoundError:
            pass
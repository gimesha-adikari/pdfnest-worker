from __future__ import annotations

import os
import shutil
from pathlib import Path
from functools import partial

import anyio
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.core.disk import check_disk_space
from app.core.subprocess_runner import run_hardened_subprocess


_office_to_pdf_capacity = anyio.CapacityLimiter(1)
_CONVERSION_TIMEOUT_SECONDS = 5 * 60
_FILTERS = {
    "docx": "pdf:writer_pdf_Export",
    "xlsx": "pdf:calc_pdf_Export",
    "pptx": "pdf:impress_pdf_Export",
}


def _cleanup_directory(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class OfficeToPdfService:
    @staticmethod
    async def convert(format: str, file: UploadFile) -> FileResponse:
        return await anyio.to_thread.run_sync(
            partial(OfficeToPdfService._convert_sync, format, file),
            limiter=_office_to_pdf_capacity,
        )

    @staticmethod
    def _convert_sync(format: str, file: UploadFile) -> FileResponse:
        conversion_filter = _FILTERS.get(format)
        if conversion_filter is None:
            raise HTTPException(status_code=400, detail=f"Unsupported Office format: {format}")

        file_size = getattr(file, "size", 0) or 10 * 1024 * 1024
        check_disk_space((file_size * 5) + (50 * 1024 * 1024))

        work_dir = Path(os.path.abspath(os.path.join("/tmp", f"office-to-pdf-{os.urandom(12).hex()}")))
        work_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        input_name = Path(file.filename or f"source.{format}").name
        input_path = work_dir / input_name
        output_path = work_dir / f"{Path(input_name).stem}.pdf"
        profile_path = work_dir / "profile"
        profile_path.mkdir(mode=0o700)

        try:
            with input_path.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            executable = shutil.which("soffice") or shutil.which("libreoffice")
            if executable is None:
                raise RuntimeError("LibreOffice executable is unavailable in the worker runtime")

            result = run_hardened_subprocess(
                [
                    executable,
                    f"-env:UserInstallation={profile_path.as_uri()}",
                    "--headless",
                    "--convert-to",
                    conversion_filter,
                    "--outdir",
                    str(work_dir),
                    str(input_path),
                ],
                cwd=str(work_dir),
                timeout=_CONVERSION_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                trace = (result.stderr or result.stdout).strip()
                raise RuntimeError(f"LibreOffice conversion failed ({result.returncode}): {trace}")
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise RuntimeError("LibreOffice did not produce a non-empty PDF artifact")
            with output_path.open("rb") as generated:
                if generated.read(5) != b"%PDF-":
                    raise RuntimeError("LibreOffice produced an invalid PDF artifact")

            return FileResponse(
                path=str(output_path),
                media_type="application/pdf",
                filename="converted.pdf",
                background=BackgroundTask(_cleanup_directory, str(work_dir)),
            )
        except HTTPException:
            _cleanup_directory(str(work_dir))
            raise
        except Exception as exc:
            _cleanup_directory(str(work_dir))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

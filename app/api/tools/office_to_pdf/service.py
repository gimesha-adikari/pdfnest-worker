from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .converters import convert_to_excel, convert_to_powerpoint, convert_to_word
from .models import OfficeOutputFormat
from .utils import cleanup_paths, create_temp_paths


class OfficeConversionService:
    @staticmethod
    async def convert(format: OfficeOutputFormat, file: UploadFile) -> FileResponse:
        temp_paths = create_temp_paths(f".{format}")
        input_path = temp_paths.input_path
        output_path = temp_paths.output_path

        try:
            with open(input_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            if format == "docx":
                convert_to_word(input_path, output_path)
                media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif format == "xlsx":
                convert_to_excel(input_path, output_path)
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            elif format == "pptx":
                convert_to_powerpoint(input_path, output_path)
                media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported format: {format}")

            download_name = f"converted.{format}"
            return FileResponse(
                path=output_path,
                media_type=media_type,
                filename=download_name,
                background=BackgroundTask(cleanup_paths, input_path, output_path),
            )
        except HTTPException:
            cleanup_paths(input_path, output_path)
            raise
        except Exception as exc:
            cleanup_paths(input_path, output_path)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

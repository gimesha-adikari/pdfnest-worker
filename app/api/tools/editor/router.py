import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from starlette.concurrency import run_in_threadpool

from app.core.editor_ocr_engine import (
    EDITOR_OCR_CONSUMER_GENERAL_EDITOR,
    EDITOR_OCR_CONSUMER_LEGACY,
    EDITOR_OCR_CONSUMER_STUDIO,
)
from app.core.storage import stream_object
from app.jobs.actors import editor_compile_job, editor_extract_job, editor_word_edit_compile_job
from app.jobs.models import JobQueue, JobState, JobSubmissionResponse
from app.jobs.store import create_job, get_job
from app.core.word_edits import (
    MAX_WORD_EDIT_OPERATIONS,
    MAX_WORD_REPLACEMENT_SCALARS,
    WordEditProtocolError,
    validate_word_edit_request,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/editor", tags=["editor"])


class ExtractRequest(BaseModel):
    source_key: str
    file_password: str | None = None
    source_name: str | None = None
    ocr_v2: bool = False
    consumer: Literal["general_editor", "studio", "legacy", "legacy_editor"] = EDITOR_OCR_CONSUMER_LEGACY
    language_mode: Literal["AUTO", "EXPLICIT"] = "EXPLICIT"
    languages: list[Literal["eng", "sin", "tam"]] = Field(default_factory=lambda: ["eng"], min_length=1, max_length=3)


class CompileRequest(BaseModel):
    source_key: str
    pages_json_key: str
    source_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_word_edit_payloads(cls, value):
        if isinstance(value, dict) and any(
            field in value for field in ("edit_protocol", "extraction_job_id", "baseline_digest", "edits")
        ):
            raise ValueError("word edit requests require the dedicated versioned protocol endpoint")
        return value


class WordEditOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    page_num: StrictInt = Field(ge=1)
    element_id: StrictStr = Field(min_length=1)
    word_id: StrictStr = Field(min_length=1)
    expected_original_text: StrictStr
    replacement_text: StrictStr = Field(max_length=MAX_WORD_REPLACEMENT_SCALARS)
    style_override: Literal[None] = Field(...)
    fit_policy: Literal["preserve_neighbors"]


class WordEditCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    edit_protocol: Literal["platen_word_edits.v1"]
    source_tracker: StrictStr = Field(min_length=1)
    extraction_job_id: StrictStr = Field(min_length=1)
    baseline_digest: StrictStr = Field(min_length=1)
    edits: list[WordEditOperationRequest] = Field(min_length=1, max_length=MAX_WORD_EDIT_OPERATIONS)


@router.post("/extract", response_model=JobSubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def extract_layout(payload: ExtractRequest) -> JobSubmissionResponse:
    if not payload.source_key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source_key is required")

    source_name = payload.source_name or Path(payload.source_key).name or "document.pdf"

    job = create_job(
        "editor_extract",
        queue_name=JobQueue.editor,
        payload=payload.model_dump(exclude_none=True),
    )

    editor_extract_job.send(
        job.id,
        payload.source_key,
        payload.file_password,
        source_name,
        payload.ocr_v2,
        payload.consumer,
        payload.language_mode,
        payload.languages,
    )
    logger.info("Dispatched extract job %s for %s", job.id, source_name)

    return JobSubmissionResponse(
        job_id=job.id,
        status=job.status.value,
        queue_name=job.queue_name.value,
    )


@router.post("/compile", response_model=JobSubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def compile_layout(payload: CompileRequest) -> JobSubmissionResponse:
    if not payload.source_key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source_key is required")
    if not payload.pages_json_key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pages_json_key is required")

    source_name = payload.source_name or Path(payload.source_key).name or "document.pdf"

    job = create_job(
        "editor_compile",
        queue_name=JobQueue.editor,
        payload=payload.model_dump(exclude_none=True),
    )

    editor_compile_job.send(job.id, payload.source_key, payload.pages_json_key, source_name)
    logger.info("Dispatched compile job %s for %s", job.id, source_name)

    return JobSubmissionResponse(
        job_id=job.id,
        status=job.status.value,
        queue_name=job.queue_name.value,
    )


@router.post("/compile-word-edits", response_model=JobSubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def compile_word_edits(payload: WordEditCompileRequest) -> JobSubmissionResponse:
    envelope = payload.model_dump(mode="python")
    extraction = get_job(payload.extraction_job_id)
    try:
        validate_word_edit_request(envelope, extraction)
    except WordEditProtocolError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    extraction_payload = extraction.payload or {}
    source_name = extraction_payload.get("source_name") or Path(payload.source_tracker).name or "document.pdf"
    owner_identity = extraction.owner_identity or extraction_payload.get("ownerIdentity")
    job_payload = {
        "source_key": payload.source_tracker,
        "source_name": source_name,
        "ownerIdentity": owner_identity or "guest:anonymous",
        "word_edit_request": envelope,
    }
    job = create_job(
        "editor_word_edit_compile",
        queue_name=JobQueue.editor,
        payload=job_payload,
        owner_identity=owner_identity,
    )
    editor_word_edit_compile_job.send(job.id, envelope)
    logger.info("Dispatched word edit validation job %s for extraction %s", job.id, payload.extraction_job_id)
    return JobSubmissionResponse(job_id=job.id, status=job.status, queue_name=job.queue_name)


@router.get("/jobs/{job_id}/download")
async def download_compiled_pdf(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.status != JobState.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is not ready for download. Current status: {job.status.value}",
        )

    result = job.result or {}
    artifact_key = result.get("artifact_key")
    if not artifact_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compiled artifact not found")

    source_name = str((job.payload or {}).get("source_name") or "document.pdf")
    download_name = result.get("artifact_name") or f"edited_{Path(source_name).stem}.pdf"

    stream, content_type = await run_in_threadpool(stream_object, artifact_key)

    return StreamingResponse(
        stream,
        media_type=content_type or "application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"'
        },
    )

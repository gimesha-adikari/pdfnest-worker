from __future__ import annotations

import mimetypes
from functools import lru_cache
from io import BytesIO
from typing import BinaryIO, Iterator
from uuid import uuid4

import boto3
from botocore.config import Config

from app.core.config import settings


class R2StorageError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_r2_client():
    if not settings.r2_bucket:
        raise R2StorageError("R2_BUCKET is missing.")
    if not settings.r2_access_key:
        raise R2StorageError("R2_ACCESS_KEY is missing.")
    if not settings.r2_secret_key:
        raise R2StorageError("R2_SECRET_KEY is missing.")
    if not settings.r2_endpoint:
        raise R2StorageError("R2_ENDPOINT is missing.")

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def build_key(prefix: str, *, suffix: str = "") -> str:
    clean_prefix = prefix.strip("/ ")
    clean_suffix = suffix.strip()

    if clean_suffix and not clean_suffix.startswith("."):
        clean_suffix = f".{clean_suffix.lstrip('.')}"

    return f"{clean_prefix}/{uuid4().hex}{clean_suffix}"


def upload_fileobj(fileobj: BinaryIO, key: str, *, content_type: str | None = None) -> str:
    client = get_r2_client()

    extra_args: dict[str, str] = {}
    if content_type:
        extra_args["ContentType"] = content_type

    if extra_args:
        client.upload_fileobj(fileobj, settings.r2_bucket, key, ExtraArgs=extra_args)
    else:
        client.upload_fileobj(fileobj, settings.r2_bucket, key)

    return key


def upload_path(path: str, key: str, *, content_type: str | None = None) -> str:
    with open(path, "rb") as f:
        return upload_fileobj(f, key, content_type=content_type)


def upload_text(text: str, key: str, *, content_type: str = "application/json") -> str:
    return upload_fileobj(BytesIO(text.encode("utf-8")), key, content_type=content_type)


def download_to_path(key: str, path: str) -> str:
    client = get_r2_client()
    client.download_file(settings.r2_bucket, key, path)
    return path


def stream_object(key: str, *, chunk_size: int = 1024 * 1024) -> tuple[Iterator[bytes], str]:
    client = get_r2_client()
    response = client.get_object(Bucket=settings.r2_bucket, Key=key)
    body = response["Body"]
    content_type = response.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"

    def iterator():
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    return iterator(), content_type
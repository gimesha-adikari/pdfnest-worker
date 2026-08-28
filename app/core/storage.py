from __future__ import annotations

import logging
import mimetypes
import os
from functools import lru_cache
from io import BytesIO
from typing import BinaryIO, Iterator
from uuid import uuid4

import boto3
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

# Dramatiq workers run outside FastAPI's startup path and need the same environment.
load_dotenv()

from app.core.config import settings

logger = logging.getLogger(__name__)

class R2StorageError(RuntimeError):
    pass

def get_encryption_key() -> bytes | None:
    key = os.environ.get("FILE_ENCRYPTION_KEY", "").strip()
    key = key.replace('"', '').replace("'", "")

    if not key:
        return None

    if len(key) != 32:
        logger.warning(f"FILE_ENCRYPTION_KEY must be exactly 32 characters. Current length: {len(key)}")
        return None

    return key.encode('utf-8')

def encrypt_data(data: bytes) -> bytes:
    key = get_encryption_key()
    if not key:
        return data
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    return nonce + ciphertext

def decrypt_data(data: bytes) -> bytes:
    if not data:
        return data

    key = get_encryption_key()

    # Legacy plaintext is accepted only when its format can be identified safely.
    is_unencrypted = (
            data.startswith(b"%PDF-") or
            data.startswith(b"{") or
            data.startswith(b"[") or
            data.startswith(b"\xff\xd8\xff") or
            data.startswith(b"\x89PNG\r\n\x1a\n") or
            data.startswith(b"RIFF") or
            data.startswith(b"GIF8") or
            data.startswith(b"II*\x00") or
            data.startswith(b"MM\x00*")
    )

    if not key:
        if is_unencrypted:
            return data
        raise RuntimeError("DECRYPTION FAILED: Python worker does not have FILE_ENCRYPTION_KEY set, but the downloaded file is encrypted. Ensure your Dramatiq worker loads the .env file.")

    if len(data) < 12:
        return data

    try:
        aesgcm = AESGCM(key)
        nonce = data[:12]
        ciphertext = data[12:]
        return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as e:
        # Do not conceal a key mismatch or corrupt ciphertext as a successful download.
        if is_unencrypted:
            return data
        raise RuntimeError(f"DECRYPTION FAILED: Key mismatch or corrupted data. Ensure FILE_ENCRYPTION_KEY is identical in Go and Python. Error: {e}")

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
            connect_timeout=2,
            read_timeout=2,
        ),
    )

def build_key(prefix: str, *, suffix: str = "") -> str:
    clean_prefix = prefix.strip("/ ")
    clean_suffix = suffix.strip()

    if clean_suffix and not clean_suffix.startswith("."):
        clean_suffix = f".{clean_suffix.lstrip('.')}"

    return f"{clean_prefix}/{uuid4().hex}{clean_suffix}"

LOCAL_STORAGE_DIR = os.environ.get("LOCAL_STORAGE_DIR", "/tmp/pdfnest-storage")

def _get_local_file_path(key: str, for_write: bool = False) -> str:
    primary = os.path.join(LOCAL_STORAGE_DIR, key.lstrip("/"))
    if for_write:
        os.makedirs(os.path.dirname(primary), exist_ok=True)
        return primary
    if os.path.exists(primary):
        return primary
    alt1 = os.path.join("/tmp", key.lstrip("/"))
    if os.path.exists(alt1):
        return alt1
    alt2 = os.path.join("/tmp", os.path.basename(key))
    if os.path.exists(alt2):
        return alt2
    os.makedirs(os.path.dirname(primary), exist_ok=True)
    return primary

def upload_fileobj(fileobj: BinaryIO, key: str, *, content_type: str | None = None) -> str:
    data = fileobj.read()
    encrypted_data = encrypt_data(data)

    if not settings.r2_bucket:
        local_path = _get_local_file_path(key, for_write=True)
        with open(local_path, "wb") as f:
            f.write(encrypted_data)
        return key

    client = get_r2_client()

    extra_args: dict[str, str] = {}
    if content_type:
        extra_args["ContentType"] = content_type

    # Use a temp file for the encrypted payload instead of wrapping in
    # BytesIO to let boto3 stream from disk and free the encrypted_data
    # buffer sooner on large files.
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".enc") as enc_f:
        enc_f.write(encrypted_data)
        enc_tmp_path = enc_f.name

    # Allow the encrypted byte buffer to be collected before upload.
    del encrypted_data

    try:
        with open(enc_tmp_path, "rb") as enc_f:
            if extra_args:
                client.upload_fileobj(enc_f, settings.r2_bucket, key, ExtraArgs=extra_args)
            else:
                client.upload_fileobj(enc_f, settings.r2_bucket, key)
    finally:
        try:
            os.remove(enc_tmp_path)
        except FileNotFoundError:
            pass

    return key

def upload_path(path: str, key: str, *, content_type: str | None = None) -> str:
    with open(path, "rb") as f:
        return upload_fileobj(f, key, content_type=content_type)

def upload_text(text: str, key: str, *, content_type: str = "application/json") -> str:
    return upload_fileobj(BytesIO(text.encode("utf-8")), key, content_type=content_type)

def download_to_path(key: str, path: str) -> str:
    if not settings.r2_bucket:
        local_path = _get_local_file_path(key)
        with open(local_path, "rb") as f:
            encrypted_data = f.read()
        decrypted_data = decrypt_data(encrypted_data)
        with open(path, "wb") as f:
            f.write(decrypted_data)
        return path

    client = get_r2_client()

    # Stream encrypted data to a temp file instead of holding it in a
    # BytesIO buffer.  AES-GCM still needs the full ciphertext for
    # authentication, but the disk-backed intermediate avoids peak RAM
    # holding encrypted + decrypted copies simultaneously.
    enc_tmp_path = path + ".enc.tmp"
    try:
        with open(enc_tmp_path, "wb") as enc_f:
            client.download_fileobj(settings.r2_bucket, key, enc_f)

        with open(enc_tmp_path, "rb") as enc_f:
            encrypted_data = enc_f.read()
    finally:
        try:
            os.remove(enc_tmp_path)
        except FileNotFoundError:
            pass

    decrypted_data = decrypt_data(encrypted_data)
    with open(path, "wb") as f:
        f.write(decrypted_data)

    return path

def stream_object(key: str, *, chunk_size: int = 1024 * 1024) -> tuple[Iterator[bytes], str]:
    import tempfile

    if not settings.r2_bucket:
        local_path = _get_local_file_path(key)
        with open(local_path, "rb") as f:
            encrypted_data = f.read()
        decrypted_data = decrypt_data(encrypted_data)
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    else:
        client = get_r2_client()
        response = client.get_object(Bucket=settings.r2_bucket, Key=key)
        content_type = response.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
        encrypted_data = response["Body"].read()
        decrypted_data = decrypt_data(encrypted_data)
        del encrypted_data

    # Write decrypted data to a temp file and stream chunks from disk
    # so the full plaintext does not remain in the Python heap during
    # the (potentially slow) HTTP response iteration.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="pdfnest-stream-")
    with os.fdopen(tmp_fd, "wb") as tmp_f:
        tmp_f.write(decrypted_data)
    del decrypted_data

    def iterator():
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass

    return iterator(), content_type

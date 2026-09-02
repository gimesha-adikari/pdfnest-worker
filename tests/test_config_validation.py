import pytest

from app.core.config import remote_storage_enabled, settings, validate_runtime_config
from app.core.storage import get_local_storage_dir


def test_managed_worker_rejects_local_fallbacks(monkeypatch):
    monkeypatch.setenv("APP_ENV", "canary")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("R2_ENDPOINT", "https://r2.example.invalid")
    monkeypatch.setenv("R2_BUCKET", "canary")
    monkeypatch.setenv("R2_ACCESS_KEY", "access")
    monkeypatch.setenv("R2_SECRET_KEY", "secret")
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "12345678901234567890123456789012")
    monkeypatch.setenv("WORKER_SHARED_SECRET", "shared")
    with pytest.raises(ValueError, match="REDIS_URL"):
        validate_runtime_config()


def test_development_storage_requires_explicit_remote_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("R2_BUCKET", "stale-development-bucket")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    assert remote_storage_enabled() is False

    monkeypatch.setenv("STORAGE_MODE", "r2")
    assert remote_storage_enabled() is True


def test_r2_client_defaults_are_bounded():
    assert settings.r2_connect_timeout_seconds == 5
    assert settings.r2_read_timeout_seconds == 30
    assert settings.r2_max_attempts == 3


def test_local_storage_default_matches_backend_contract(monkeypatch):
    monkeypatch.delenv("LOCAL_STORAGE_DIR", raising=False)
    assert get_local_storage_dir().endswith("/pdfnest-storage")


def test_runtime_config_rejects_invalid_searchable_pdf_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.setenv("SEARCHABLE_PDF_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="SEARCHABLE_PDF_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_document_extraction_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.setenv("DOCUMENT_EXTRACTION_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="DOCUMENT_EXTRACTION_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_pdf_to_markdown_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.setenv("PDF_TO_MARKDOWN_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="PDF_TO_MARKDOWN_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_pdf_to_word_ocr_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_MARKDOWN_ENGINE", raising=False)
    monkeypatch.setenv("PDF_TO_WORD_OCR_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="PDF_TO_WORD_OCR_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_ocr_markup_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_MARKDOWN_ENGINE", raising=False)
    monkeypatch.setenv("OCR_MARKUP_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="OCR_MARKUP_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_editor_ocr_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_MARKDOWN_ENGINE", raising=False)
    monkeypatch.delenv("OCR_MARKUP_ENGINE", raising=False)
    monkeypatch.setenv("EDITOR_OCR_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="EDITOR_OCR_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_legacy_editor_ocr_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_MARKDOWN_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_WORD_OCR_ENGINE", raising=False)
    monkeypatch.delenv("OCR_MARKUP_ENGINE", raising=False)
    monkeypatch.delenv("EDITOR_OCR_ENGINE", raising=False)
    monkeypatch.setenv("LEGACY_EDITOR_OCR_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="LEGACY_EDITOR_OCR_ENGINE"):
        validate_runtime_config()


def test_runtime_config_rejects_invalid_legacy_markup_ocr_engine_selector(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_ENGINE", raising=False)
    monkeypatch.delenv("SEARCHABLE_PDF_ENGINE", raising=False)
    monkeypatch.delenv("DOCUMENT_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_MARKDOWN_ENGINE", raising=False)
    monkeypatch.delenv("PDF_TO_WORD_OCR_ENGINE", raising=False)
    monkeypatch.delenv("OCR_MARKUP_ENGINE", raising=False)
    monkeypatch.delenv("EDITOR_OCR_ENGINE", raising=False)
    monkeypatch.delenv("LEGACY_EDITOR_OCR_ENGINE", raising=False)
    monkeypatch.setenv("LEGACY_MARKUP_OCR_ENGINE", "not-an-engine")

    with pytest.raises(ValueError, match="LEGACY_MARKUP_OCR_ENGINE"):
        validate_runtime_config()

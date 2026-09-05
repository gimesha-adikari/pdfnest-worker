"""Test-only environment isolation for consumer engine selectors."""

import os


ENGINE_SELECTOR_ENV_VARS = (
    "OCR_TEXT_ENGINE",
    "SEARCHABLE_PDF_ENGINE",
    "DOCUMENT_EXTRACTION_ENGINE",
    "PDF_TO_MARKDOWN_ENGINE",
    "PDF_TO_WORD_OCR_ENGINE",
    "OCR_MARKUP_ENGINE",
    "EDITOR_OCR_ENGINE",
    "STUDIO_EDITOR_EXTRACTION_ENGINE",
    "STUDIO_MARKUP_REGION_OCR_ENGINE",
    "LEGACY_EDITOR_OCR_ENGINE",
    "LEGACY_MARKUP_OCR_ENGINE",
)


def pytest_configure(config) -> None:
    """Keep selector tests independent from a developer's ignored .env.

    Branch-specific tests must set their selector explicitly with pytest's
    environment tools.  This default is applied before test collection so
    importing application configuration cannot load a developer-selected
    branch accidentally.
    """

    del config
    for variable in ENGINE_SELECTOR_ENV_VARS:
        os.environ[variable] = "internal"

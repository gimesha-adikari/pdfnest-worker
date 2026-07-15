from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .document import compile_document, extract_document


@dataclass
class EditorService:
    def extract_layout(self, pdf_path: str, password: Optional[str] = None) -> dict:
        return extract_document(pdf_path, password)

    def compile_layout(self, original_pdf: str, output_pdf: str, pages_json_path: str) -> None:
        compile_document(original_pdf, output_pdf, pages_json_path)

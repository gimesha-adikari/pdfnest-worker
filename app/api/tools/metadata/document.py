from __future__ import annotations

from typing import Optional

from .utils import open_document


def read_metadata(
        input_path: str,
        password: Optional[str] = None,
) -> dict:
    doc = open_document(input_path, password)

    try:
        meta = doc.metadata or {}

        return {
            "title": (meta.get("title") or "").strip(),
            "author": (meta.get("author") or "").strip(),
            "subject": (meta.get("subject") or "").strip(),
            "keywords": (meta.get("keywords") or "").strip(),
        }

    finally:
        doc.close()


def write_metadata(
        input_path: str,
        output_path: str,
        title: str,
        author: str,
        subject: str,
        keywords: str,
        password: Optional[str] = None,
):
    doc = open_document(input_path, password)

    try:
        meta = doc.metadata or {}

        meta["title"] = title.strip()
        meta["author"] = author.strip()
        meta["subject"] = subject.strip()
        meta["keywords"] = keywords.strip()

        doc.set_metadata(meta)

        doc.save(
            output_path,
            garbage=3,
            deflate=True,
        )

    finally:
        doc.close()
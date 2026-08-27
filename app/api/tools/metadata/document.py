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


def preserve_metadata(
        input_path: str,
        metadata_source_path: str,
        output_path: str,
        password: Optional[str] = None,
        source_password: Optional[str] = None,
):
    """Copy the source PDF metadata envelope onto an assembled PDF.

    Studio assembles pages into a new PDF before applying its public metadata
    fields.  This keeps source-only Info entries and XMP available for the
    final metadata write without exposing them in the Studio VDM.
    """
    source = open_document(metadata_source_path, source_password)
    target = open_document(input_path, password)

    try:
        target.set_metadata(source.metadata or {})
        xml_metadata = source.get_xml_metadata()
        if xml_metadata:
            target.set_xml_metadata(xml_metadata)
        target.save(output_path, garbage=3, deflate=True)
    finally:
        target.close()
        source.close()

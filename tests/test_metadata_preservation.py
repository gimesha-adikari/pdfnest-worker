from __future__ import annotations

import fitz

from app.api.tools.metadata.document import preserve_metadata, read_metadata, write_metadata


def make_metadata_pdf(path):
    doc = fitz.open()
    doc.new_page()
    doc.set_metadata({
        "title": "Source title",
        "author": "Source author",
        "subject": "Source subject",
        "keywords": "one, two",
        "creator": "Original creator",
        "producer": "Original producer",
        "creationDate": "D:20200101000000Z",
        "modDate": "D:20210101000000Z",
    })
    doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><x:tool>u6</x:tool></x:xmpmeta>')
    doc.save(path)
    doc.close()


def test_read_metadata_exposes_normalized_visible_fields(tmp_path):
    source = tmp_path / "source.pdf"
    make_metadata_pdf(source)
    assert read_metadata(str(source)) == {
        "title": "Source title",
        "author": "Source author",
        "subject": "Source subject",
        "keywords": "one, two",
    }


def test_write_metadata_preserves_unexposed_info_and_xmp(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    make_metadata_pdf(source)

    write_metadata(str(source), str(output), "Source title", "Edited author", "", "one, two")

    doc = fitz.open(output)
    metadata = doc.metadata
    assert metadata["title"] == "Source title"
    assert metadata["author"] == "Edited author"
    assert metadata["subject"] == ""
    assert metadata["keywords"] == "one, two"
    assert metadata["creator"] == "Original creator"
    assert metadata["producer"] == "Original producer"
    assert metadata["creationDate"] == "D:20200101000000Z"
    assert metadata["modDate"] == "D:20210101000000Z"
    assert "<x:tool>u6</x:tool>" in doc.get_xml_metadata()
    doc.close()


def test_write_metadata_empty_source_creates_empty_visible_fields(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(source)
    doc.close()

    write_metadata(str(source), str(output), "", "", "", "")

    doc = fitz.open(output)
    assert all(doc.metadata.get(key, "") == "" for key in ("title", "author", "subject", "keywords"))
    doc.close()


def test_preserve_metadata_copies_source_envelope_to_assembled_pdf(tmp_path):
    source = tmp_path / "source.pdf"
    assembled = tmp_path / "assembled.pdf"
    output = tmp_path / "output.pdf"
    make_metadata_pdf(source)

    doc = fitz.open()
    doc.new_page()
    doc.save(assembled)
    doc.close()

    preserve_metadata(str(assembled), str(source), str(output))

    result = fitz.open(output)
    assert result.metadata["creator"] == "Original creator"
    assert result.metadata["producer"] == "Original producer"
    assert "<x:tool>u6</x:tool>" in result.get_xml_metadata()
    result.close()

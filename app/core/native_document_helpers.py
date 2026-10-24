"""Document preparation/projection operations executed only by native_child."""


def inspect_pdf(path, max_pages, cancellation_check=None):
    import pymupdf as fitz
    from fastapi import HTTPException
    try:
        with fitz.open(path) as document:
            if len(document) == 0 or len(document) > max_pages:
                raise HTTPException(413, detail={'code': 'INVALID_INPUT', 'message': 'OCR V2 document exceeds the configured page limit'})
            return len(document)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(415, detail={'code': 'INVALID_INPUT', 'message': 'Uploaded PDF could not be read'}) from exc


def preview_source(input_path, output_path, page_index, cancellation_check=None):
    import pymupdf as fitz
    from fastapi import HTTPException
    try:
        with fitz.open(input_path) as source:
            page_count = len(source)
            if not 0 <= page_index < page_count:
                raise HTTPException(422, detail={'code': 'INVALID_INPUT', 'message': 'The requested preview page is unavailable'})
            with fitz.open() as selected:
                selected.insert_pdf(source, from_page=page_index, to_page=page_index)
                selected.save(output_path, garbage=3, deflate=True)
        return page_count
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(415, detail={'code': 'INVALID_INPUT', 'message': 'This PDF page could not be prepared for preview'}) from exc


def image_source_metadata(image_paths, output_pdf, **kwargs):
    from dataclasses import fields
    from app.core.ocr_v2.image_pages import build_image_source_pdf
    # PNG bytes remain in the child. Actors need only diagnostic metadata.
    return [{field.name: getattr(image, field.name) for field in fields(image) if field.name != 'png_bytes'}
            for image in build_image_source_pdf(image_paths, output_pdf, **kwargs)]


def editor_colors(layout, source_path, password=None, **kwargs):
    from app.core.editor_ocr_projection import preserve_native_editor_colors
    preserve_native_editor_colors(layout, source_path, password, **kwargs)
    return layout


def structured_document(input_path, **kwargs):
    from app.core.ocr_v2.structured import StructuredDocumentProcessor
    return StructuredDocumentProcessor().process_document(input_path, **kwargs)

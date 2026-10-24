"""Explicit OCR operation allowlist; storage, job state and billing stay in parent."""
from functools import partial
from app.core.native_isolation import execute

# operation -> (module, function, optional file-output argument)
OPERATIONS = {
    name: ('app.core.' + module, name, output)
    for name, module, output in (
        ('execute_ocr_text', 'ocr_text_engine', None),
        ('execute_searchable_pdf', 'searchable_pdf_engine', 'output_pdf'),
        ('execute_document_extraction', 'document_extraction_engine', None),
        ('execute_pdf_to_markdown', 'pdf_to_markdown_engine', None),
        ('execute_ocr_markup', 'ocr_markup_engine', 'output_path'),
        ('execute_ocr_markup_preview', 'ocr_markup_engine', None),
        ('execute_editor_ocr', 'editor_ocr_engine', None),
        ('execute_legacy_editor_ocr', 'legacy_editor_ocr_engine', None),
        ('execute_studio_editor_extraction', 'studio_editor_extraction_engine', None),
        ('execute_studio_markup_region_ocr', 'studio_markup_region_ocr_engine', 'output_path'),
        ('execute_legacy_markup', 'legacy_markup_ocr_engine', 'output_path'),
    )
}
OPERATIONS.update({
    'extract_text_from_pdf': ('app.api.tools.ocr.document', 'extract_text_from_pdf', 'output_path'),
    'build_searchable_pdf_from_images': ('app.api.tools.ocr.document', 'build_searchable_pdf_from_images', 'output_path'),
    'extract_document': ('app.api.tools.editor.document', 'extract_document', None),
    'extract_document_v2': ('app.api.tools.editor.document', 'extract_document_v2', None),
    'compile_document': ('app.api.tools.editor.document', 'compile_document', 'output_pdf_path'),
    'process_markup_pdf': ('app.api.tools.markup.document', 'process_markup_pdf', 'output_path'),
    'convert_pdf_to_markdown': ('app.api.tools.markdown.service', 'convert_pdf_to_markdown', None),
    'convert_to_word': ('app.api.tools.pdf_to_office.converters.word', 'convert_to_word', 'output_path'),
    'image_source_metadata': ('app.core.native_document_helpers', 'image_source_metadata', 'output_pdf'),
    'editor_colors': ('app.core.native_document_helpers', 'editor_colors', None),
    'inspect_pdf': ('app.core.native_document_helpers', 'inspect_pdf', None),
    'preview_source': ('app.core.native_document_helpers', 'preview_source', 'output_path'),
    'structured_document': ('app.core.native_document_helpers', 'structured_document', None),
})

for _operation in OPERATIONS:
    globals()[_operation] = partial(execute, _operation)

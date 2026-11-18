"""Independent refusal controls. These tests are not browser qualification."""
from hashlib import sha256
import pymupdf as fitz
from app.core.scanned_word_compiler import compile_scanned_word_edits
from tests.test_scanned_word_compiler import _fixture_pdf,_request
from tests.test_word_edits_script_compilation import _searchable_source_pdf


def _run(source,output,bbox):
    envelope,targets,_=_request(bbox)
    source_sha=sha256(source.read_bytes()).hexdigest();targets[0]['source_sha256']=source_sha
    result=compile_scanned_word_edits(source,output,envelope,targets,source_sha)
    assert not output.exists()
    assert sha256(source.read_bytes()).hexdigest()==source_sha
    return result


def test_sheared_image_occurrence_is_refused_without_artifact(tmp_path):
    source=tmp_path/'shear.pdf'; initial=tmp_path/'initial.pdf';output=tmp_path/'output.pdf'
    bbox=_fixture_pdf(initial)
    with fitz.open(initial) as doc:
        p=doc[0];changed=False
        for x in p.get_contents():
            stream=doc.xref_stream(x)
            altered=stream.replace(b'595 0 0 842 0 0 cm',b'595 0 20 842 0 0 cm')
            if stream!=altered:doc.update_stream(x,altered);changed=True
        assert changed
        doc.save(source)
    assert _run(source,output,bbox)['error_code']=='UNSUPPORTED_IMAGE_TRANSFORM'


def test_misaligned_hidden_ocr_remains_strictly_refused(tmp_path):
    initial=tmp_path/'aligned.pdf';source=tmp_path/'shifted.pdf';output=tmp_path/'output.pdf'
    bbox=_searchable_source_pdf(initial)
    with fitz.open(initial) as doc:
        p=doc[0];changed=False
        for x in p.get_contents():
            stream=doc.xref_stream(x)
            if b'3 Tr' in stream:
                altered=stream.replace(b'1 0 0 1 60 ',b'1 0 0 1 65 ')
                assert altered!=stream;doc.update_stream(x,altered);changed=True
        assert changed;doc.save(source)
    assert _run(source,output,bbox)['error_code']=='HIDDEN_OCR_ALIGNMENT_UNCERTAIN'

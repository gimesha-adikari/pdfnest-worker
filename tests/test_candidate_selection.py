"""Candidate-derived word selection must never trust an editor string or OCR box."""
from hashlib import sha256
from pathlib import Path

import pymupdf as fitz
import pytest

from app.core.candidate_selection import inspect_candidate_selection
from app.core.scanned_word_compiler import compile_scanned_word_edits


@pytest.fixture
def candidate(tmp_path):
    def make(replacement='අධ්‍යාපනය',searchable=False):
        source=tmp_path/'source.pdf'; output=tmp_path/(str(len(list(tmp_path.iterdir())))+'-candidate.pdf')
        with fitz.open() as d:
            p=d.new_page(width=595,height=842)
            p.insert_text((60,120),'Ministry          of Education, Higher',fontsize=18)
            bbox=list(next(w[:4] for w in p.get_text('words') if w[4]=='Ministry'))
            control=d.new_page(width=595,height=842)
            control.insert_text((60,120),'Control page unchanged.',fontsize=18)
            d.save(source)
        if searchable:
            from tests.test_word_edits_script_compilation import _searchable_source_pdf
            bbox=_searchable_source_pdf(source)
        digest=sha256(source.read_bytes()).hexdigest()
        edit={'page_num':1,'element_id':'e','word_id':'w','expected_original_text':'Ministry','replacement_text':replacement,'style_override':None,'fit_policy':'preserve_neighbors'}
        env={'edit_protocol':'platen_word_edits.v1','source_tracker':'owned','extraction_job_id':'extract','baseline_digest':'sha256:'+'1'*64,'edits':[edit]}
        targets=[{'edit':edit,'bbox':bbox,'page_size':[595.,842.],'source_sha256':digest,'baseline_digest':env['baseline_digest']}]
        result=compile_scanned_word_edits(source,output,env,targets,digest)
        assert result['status']=='EDITED',result
        return output
    return make


@pytest.mark.parametrize('text',['සිංහල','කී','ශ්‍රී','අධ්‍යාපනය','අධ්යාපනය','கி','கல்வி','தமிழ்நாடு'])
def test_script_word_matches_exact_candidate_patch_without_scaling(candidate,text):
    path=candidate(text); digest=sha256(path.read_bytes()).hexdigest()
    result=inspect_candidate_selection(str(path),digest)
    assert result['artifact_sha256']==digest
    words=[w for p in result['pages'] for w in p['words']]
    word=next(w for w in words if w['text']==text)
    assert word['representation']=='verified_raster_word'
    assert word['selection']=='whole_word'
    with fitz.open(path) as d:
        image=next(i for i in d[0].get_images() if i[1])
        assert word['rect']==pytest.approx(list(d[0].get_image_rects(image[0])[0]))
    assert sha256(path.read_bytes()).hexdigest()==digest


def test_wrong_artifact_digest_exposes_no_text(candidate):
    with pytest.raises(ValueError,match='identity'):
        inspect_candidate_selection(str(candidate()),'0'*64)


@pytest.mark.parametrize('text',['කී','கி'])
def test_clean_searchable_word_uses_exact_rgb_patch_and_actual_ink_location(candidate,text):
    path=candidate(text,searchable=True)
    result=inspect_candidate_selection(str(path),sha256(path.read_bytes()).hexdigest())
    word=next(w for w in result['pages'][0]['words'] if w['text']==text)
    assert word['representation']=='verified_raster_word'
    with fitz.open(path) as doc:
        patches=[r for im in doc[0].get_images() if im[2]*im[3]<1_000_000 for r in doc[0].get_image_rects(im[0])]
        assert len(patches)==1 and patches[0].contains(fitz.Rect(word['rect']))


def test_rgb_patch_with_unrelated_ink_is_not_exposed(candidate,tmp_path):
    path=candidate('කී',searchable=True);changed=tmp_path/'dirty-patch.pdf'
    with fitz.open(path) as doc:
        p=doc[0];image=next(im for im in p.get_images() if im[2]*im[3]<1_000_000)
        pix=fitz.Pixmap(doc,image[0]);pix.set_pixel(0,0,(0,0,0));p.replace_image(image[0],pixmap=pix);doc.save(changed)
    result=inspect_candidate_selection(str(changed),sha256(changed.read_bytes()).hexdigest())
    assert not any(w['representation']=='verified_raster_word' for p in result['pages'] for w in p['words'])


def test_unmatched_hidden_text_is_not_exposed(tmp_path):
    path=tmp_path/'hidden.pdf'
    with fitz.open() as d:
        p=d.new_page();p.insert_text((60,120),'secret',render_mode=3);d.save(path)
    result=inspect_candidate_selection(str(path),sha256(path.read_bytes()).hexdigest())
    assert result['pages'][0]['words']==[]


@pytest.mark.parametrize('mutation',['moved','reused','reused_other_page','altered','overlap','annotation','visible_conflict'])
def test_script_patch_must_be_exact_unique_aligned_and_unobscured(candidate,tmp_path,mutation):
    path=candidate(); changed=tmp_path/(mutation+'.pdf')
    with fitz.open(path) as d:
        p=d[0]; image=next(i for i in p.get_images() if i[1]);rect=p.get_image_rects(image[0])[0]
        if mutation=='reused':p.insert_image(fitz.Rect(200,200,200+rect.width,200+rect.height),xref=image[0])
        elif mutation=='reused_other_page':d[1].insert_image(rect,xref=image[0])
        elif mutation=='moved':
            # A shifted hidden text with an otherwise identical image must not match.
            updated = False
            for x in p.get_contents():
                stream=d.xref_stream(x)
                if b'3 Tr' in stream:
                    changed_stream = stream.replace(b'1 0 0 1 60 ', b'1 0 0 1 70 ')
                    assert changed_stream != stream
                    d.update_stream(x,changed_stream)
                    updated = True
            assert updated
        elif mutation=='altered':
            pix=fitz.Pixmap(d,image[0]);pix.set_pixel(0,0,(255,0,0));p.replace_image(image[0],pixmap=pix)
        elif mutation=='overlap':p.draw_line(rect.tl,rect.br,color=(1,0,0))
        elif mutation=='annotation':p.add_rect_annot(rect)
        elif mutation=='visible_conflict':p.insert_text((rect.x0,rect.y1),'X',fontsize=12)
        d.save(changed)
    result=inspect_candidate_selection(str(changed),sha256(changed.read_bytes()).hexdigest())
    assert not any(w['representation']=='verified_raster_word' for p in result['pages'] for w in p['words'])


def test_native_visible_ascii_words_are_separate_in_reading_order(candidate):
    path=candidate('hello'); result=inspect_candidate_selection(str(path),sha256(path.read_bytes()).hexdigest())
    assert [w['text'] for w in result['pages'][0]['words']]==['hello','of','Education,','Higher']
    assert all(w['representation']=='native_visible_word' for w in result['pages'][0]['words'])


@pytest.mark.parametrize('mutation',['rotate','crop','clip','font'])
def test_unqualified_geometry_clipping_and_native_fonts_are_not_exposed(tmp_path,mutation):
    path=tmp_path/(mutation+'.pdf')
    with fitz.open() as d:
        p=d.new_page(width=595,height=842)
        p.insert_text((60,120),'visible',fontname='helv',fontsize=18)
        if mutation=='rotate':p.set_rotation(90)
        if mutation=='crop':p.set_cropbox(fitz.Rect(10,10,585,832))
        if mutation=='clip':
            x=p.get_contents()[0];d.update_stream(x,b'q 0 0 1 1 re W n\n'+d.xref_stream(x)+b'\nQ')
        if mutation=='font':
            # Unqualified embedded fonts are deliberately outside native selection.
            p.insert_text((60,160),'embedded',fontname='noto',fontfile='/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf',fontsize=18)
        d.save(path)
    words=inspect_candidate_selection(str(path),sha256(path.read_bytes()).hexdigest())['pages'][0]['words']
    assert not words if mutation!='font' else all(w['text']!='embedded' for w in words)

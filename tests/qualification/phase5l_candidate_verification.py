"""Reproduce local Phase 5L PDF gates; generated documents stay outside Git."""
from pathlib import Path
from hashlib import sha256
from tempfile import TemporaryDirectory
import argparse,json,math,subprocess
import pymupdf as fitz
from PIL import Image,ImageChops,ImageDraw
from app.core.scanned_word_compiler import compile_scanned_word_edits,_prepare_target
from app.core.candidate_selection import inspect_candidate_selection
from tests.test_word_edits_script_compilation import _source_pdf,_searchable_source_pdf
from tests.test_scanned_word_compiler import _fixture_pdf

CASES=[('sinhala-simple','native','සිංහල'),('sinhala-vowel','native','කී'),('sinhala-conjunct','native','ශ්‍රී'),('sinhala-joiner','native','අධ්‍යාපනය'),('sinhala-without-joiner','native','අධ්යාපනය'),('tamil-vowel','native','கி'),('tamil-cluster','native','கல்வி'),('tamil-word','native','தமிழ்நாடு'),('sinhala-searchable','searchable','කී'),('tamil-searchable','searchable','கி'),('english-native','native','hello'),('english-scan','scan','hello')]
def digest(p):return sha256(p.read_bytes()).hexdigest()
def image(page,dpi,renderer,pdf,page_num):
    if renderer=='MuPDF':
        pix=page.get_pixmap(dpi=dpi,alpha=False,colorspace=fitz.csRGB)
        return Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
    with TemporaryDirectory(prefix='platen-pixel-') as td:
        prefix=Path(td)/'render'
        subprocess.run(['pdftoppm','-f',str(page_num),'-l',str(page_num),'-r',str(dpi),'-png','-singlefile',str(pdf),str(prefix)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        with Image.open(str(prefix)+'.png') as im:return im.convert('RGB')
def compare(a,b,mask,dpi):
    assert a.size==b.size
    diff=ImageChops.difference(a,b);r,g,blue=diff.split();delta=ImageChops.lighter(ImageChops.lighter(r,g),blue)
    changed=delta.point(lambda v:255 if v>2 else 0)
    allowed=Image.new('L',a.size,0)
    if mask:
        x0,y0,x1,y1=mask;s=dpi/72
        # Frozen point mask, plus exactly one antialias/raster pixel. Never output-derived.
        ImageDraw.Draw(allowed).rectangle((max(0,math.floor(x0*s)-1),max(0,math.floor(y0*s)-1),min(a.width,math.ceil(x1*s)+1)-1,min(a.height,math.ceil(y1*s)+1)-1),fill=255)
    outside=ImageChops.subtract(changed,allowed)
    return {'size':a.size,'changed':changed.histogram()[255],'outside':outside.histogram()[255],'max_channel_delta':max(v[1] for v in diff.getextrema())}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args();root=Path(args.output).resolve()
    if any((parent/'.git').exists() for parent in (root,*root.parents)):
        raise ValueError('Artifacts must stay outside Git repositories')
    root.mkdir(parents=True,exist_ok=True);results=[]
    for case,kind,text in CASES:
        out=root/case;out.mkdir();source=out/'source.pdf';candidate=out/'candidate.pdf'
        bbox=(_source_pdf if kind=='native' else _searchable_source_pdf if kind=='searchable' else _fixture_pdf)(source)
        # Add a known unedited control page, preserving source page 1.
        with fitz.open(source) as d:
            p=d.new_page(width=595,height=842);p.insert_text((60,120),'Control page unchanged.',fontsize=18);data=d.tobytes()
        source.write_bytes(data);source_sha=digest(source)
        edit={'page_num':1,'element_id':'fixture-line','word_id':'fixture-word','expected_original_text':'Ministry','replacement_text':text,'style_override':None,'fit_policy':'preserve_neighbors'}
        env={'edit_protocol':'platen_word_edits.v1','source_tracker':'synthetic-owned','extraction_job_id':'synthetic-extract','baseline_digest':'sha256:'+'1'*64,'edits':[edit]}
        target={'edit':edit,'bbox':bbox,'page_size':[595.,842.],'source_sha256':source_sha,'baseline_digest':env['baseline_digest']}
        with fitz.open(source) as d:
            plan,reason=_prepare_target(d,target);assert not reason,reason
            mask=list(plan['allowed_rect'])
        declared={'source_sha256':source_sha,'bbox':bbox,'mask':mask,'threshold':2,'raster_band_px':1,'declared_before_compile':True}
        (out/'declared.json').write_text(json.dumps(declared,indent=2))
        result=compile_scanned_word_edits(source,candidate,env,[target],source_sha);assert result['status']=='EDITED',result
        candidate_sha=digest(candidate)
        selection=inspect_candidate_selection(str(candidate),candidate_sha)
        (out/'selection.json').write_text(json.dumps(selection,ensure_ascii=False,indent=2))
        qpdf=subprocess.run(['qpdf','--check',str(candidate)],capture_output=True,text=True);assert qpdf.returncode==0,qpdf.stderr
        poppler=subprocess.check_output(['pdftotext','-enc','UTF-8',str(candidate),'-'],text=True)
        with fitz.open(source) as original,fitz.open(candidate) as edited:
            assert len(original)==len(edited)==2
            assert all((a.mediabox,a.cropbox,a.rotation)==(b.mediabox,b.cropbox,b.rotation) for a,b in zip(original,edited))
            extracted=edited[0].get_text();assert extracted.count(text)==1 and 'Ministry' not in extracted
            assert poppler.count(text)==1 and 'Ministry' not in poppler
            assert len(edited[0].search_for(text))==1
            for a in original:
                for i in a.get_images():
                    stream=original.xref_stream_raw(i[0]);assert any(edited.xref_stream_raw(j[0])==stream for p in edited for j in p.get_images())
            edited[0].get_pixmap(dpi=144,alpha=False).save(out/'page.png')
            measurements=[]
            for renderer in ['MuPDF','Poppler']:
                for dpi in [72,144,288]:
                    for index in range(2):
                        values=compare(image(original[index],dpi,renderer,source,index+1),image(edited[index],dpi,renderer,candidate,index+1),mask if index==0 else None,dpi)
                        assert values['outside']==0,(case,renderer,dpi,index,values)
                        if index==1:assert values['changed']==0
                        measurements.append({'renderer':renderer,'dpi':dpi,'page':index+1,**values})
        assert digest(candidate)==candidate_sha
        record={'case':case,'kind':kind,'text':text,'codepoints':[f'U+{ord(c):04X}' for c in text],'source_sha256':source_sha,'candidate_sha256':candidate_sha,'mask':mask,'qpdf':True,'mupdf_exact':True,'poppler_exact':True,'search_hits':1,'selection_representations':[(w['text'],w['representation']) for p in selection['pages'] for w in p['words'] if w['text']==text],'measurements':measurements}
        results.append(record);print(case,candidate_sha,'outside=0 for 2 renderers x 3 DPI x 2 pages',flush=True)
        (root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

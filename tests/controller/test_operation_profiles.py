"""Real operations over adapter/broker/Unix transport; synthetic manager only.

The native subprocess uses the application's actual shared dispatcher and real
small PDF/image fixtures. This proves compatibility, never kernel containment.
"""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
from threading import Thread
import pytest
import pymupdf
from PIL import Image, ImageDraw
from app.controller.profiles import PROFILES, ROOT, validate_manifest, validate_result
from app.core import native_domain as d, native_operations as operations, native_isolation as boundary


class OperationManager:
    """Ordinary-UID test double; explicitly no cgroup/namespace/UID claims."""
    def __init__(self,root):
        self.root=root; self.jobs={}; self.completed={}; self.cleaned=[]
    def preflight(self): pass
    def inventory(self): return []
    def prepare(self,lease,manifest,deadline):
        validate_manifest(manifest)
        directory=self.root/lease.domain
        directory.mkdir(mode=0o700)
        source=directory/'input';source.mkdir()
        (source/'request.json').write_bytes(manifest['request'])
        for name,data in manifest['files'].items(): (source/name).write_bytes(data)
        self.jobs[lease.domain]={'directory':directory,'sequence':0,'child':None}
        return {'token':'synthetic-gate','invocation':'a'*32,'cgroup':'/synthetic'}
    def grant(self,lease,token,deadline):
        job=self.jobs[lease.domain]; root=job['directory']
        code='from app.controller.dispatcher import main;from pathlib import Path;main(Path('+repr(str(root/'input'))+'),Path('+repr(str(root/'job'))+'))'
        env={'PATH':os.environ['PATH'],'PYTHONPATH':str(Path.cwd()),'LANG':'C.UTF-8','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','OCR_TEXT_ENGINE':'internal','SEARCHABLE_PDF_ENGINE':'internal','DOCUMENT_EXTRACTION_ENGINE':'internal','PDF_TO_MARKDOWN_ENGINE':'internal','OCR_MARKUP_ENGINE':'internal','EDITOR_OCR_ENGINE':'internal','LEGACY_EDITOR_OCR_ENGINE':'internal','STUDIO_EDITOR_EXTRACTION_ENGINE':'internal','STUDIO_MARKUP_REGION_OCR_ENGINE':'internal','LEGACY_MARKUP_OCR_ENGINE':'internal'}
        job['child']=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def poll(self,lease,deadline):
        job=self.jobs[lease.domain]; root=job['directory']/'job'
        if (root/'event.json').exists(): return {'event':json.loads((root/'event.json').read_bytes())}
        if job['child'].poll() is None:return None
        if lease.domain not in self.completed:
            artifacts={p.name:p.read_bytes() for p in root.iterdir() if p.name in ('result.json','output.pdf','output.txt','output.docx')}
            result={'returncode':job['child'].wait(),'stdout':'','stderr':'','reaped':True,'artifacts':artifacts}
            validate_result(result);self.completed[lease.domain]=result
        return self.completed[lease.domain]
    def ack(self,lease,sequence,deadline):
        job=self.jobs[lease.domain];assert sequence==job['sequence']+1
        (job['directory']/'job'/'event.json').unlink();job['sequence']=sequence
    def drain(self,lease,deadline):
        job=self.jobs[lease.domain]
        if job['child'].poll() is None:
            job['child'].kill();job['child'].wait()
            # Cancellation has no semantic result. The real manager must still
            # independently drain the protected domain before reuse.
            return {'returncode':-9,'stdout':'','stderr':'','reaped':True}
        return self.poll(lease,deadline)
    def cleanup(self,lease,deadline):
        if hasattr(self,'publication'):assert not self.publication.exists(), 'publication must follow release acknowledgement'
        import shutil
        shutil.rmtree(self.jobs[lease.domain]['directory']);self.cleaned.append(lease.operation)
    def abort(self,lease,deadline): self.drain(lease,deadline)


@pytest.fixture
def controller(tmp_path,monkeypatch):
    from app.controller.transport import Client,Server,broker_handler
    from app.controller.broker import Broker,Journal
    from app.controller.adapter import WorkerController
    identity=d.Identity('synthetic-operations','e1','a'*64)
    limits=d.Limits(1024**3,0,32,50000,100000,300)
    root=tmp_path/'manager';root.mkdir()
    manager=OperationManager(root)
    broker=Broker(Journal(tmp_path/'state.sqlite',identity,('one',),128),manager,limits)
    endpoint=tmp_path/'socket';listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(str(endpoint));listener.listen(1)
    server=Server(identity,(os.getuid(),),broker_handler(broker),broker.disconnect,2)
    def serve():
        connection,_=listener.accept();server.connection(connection)
    thread=Thread(target=serve);thread.start()
    client=Client(endpoint,os.getuid(),identity)
    registration=d.Registration(WorkerController(client),identity,limits,('one',))
    monkeypatch.setattr(d,'_registration',registration)
    monkeypatch.setenv('NATIVE_EXECUTION_DOMAIN_MODE','required-nonproduction')
    yield broker,manager,registration
    client.close();thread.join(timeout=2);listener.close();broker.journal.close()
    assert not thread.is_alive()


@pytest.fixture
def fixtures(tmp_path):
    pdf=tmp_path/'small.pdf'
    with pymupdf.open() as doc:
        page=doc.new_page(width=300,height=200)
        import io
        background=io.BytesIO();Image.new('RGB',(300,200),'white').save(background,format='PNG')
        page.insert_image(page.rect,stream=background.getvalue())
        page.insert_text((20,40),'F11 OPERATION FIXTURE',fontsize=14);doc.save(pdf)
    image=tmp_path/'small.png';im=Image.new('RGB',(600,400),'white');ImageDraw.Draw(im).text((40,80),'F11 IMAGE FIXTURE',fill='black');im.save(image)
    layout=tmp_path/'layout.json';layout.write_text('{"pages": []}')
    return pdf,image,layout


def arguments(operation,fixtures,tmp_path):
    pdf,image,layout=fixtures
    output=tmp_path/('published.txt' if operation=='extract_text_from_pdf' else 'published.docx' if operation=='convert_to_word' else 'published.pdf')
    p=PROFILES[operation];values={}
    for key in p.inputs: values[key]=[str(image)] if key=='image_paths' else str(layout) if key=='pages_json_path' else str(pdf)
    if p.output: values[p.output]=str(output)
    if 'language' in p.required:values['language']='eng'
    if 'action' in p.required:values['action']='highlight'
    if 'boxes' in p.required:values['boxes']=[{'page':1,'x':15,'y':20,'width':250,'height':30}]
    if 'max_pages' in p.required:values['max_pages']=10
    if 'page_index' in p.required:values['page_index']=0
    if 'layout' in p.required:values['layout']={'pages':[{'source':'NATIVE_EXTRACTION','page_num':1,'elements':[{'x':15,'y':20,'width':260,'height':35}]}]}
    if operation=='execute_ocr_markup':values['query']='F11'
    if operation in ('execute_ocr_text','execute_document_extraction','execute_pdf_to_markdown','structured_document'):values['routing_policy']='FAST'
    return values,output


@pytest.mark.parametrize('operation',tuple(PROFILES))
def test_all24_actual_dispatcher_roundtrip_and_release(controller,fixtures,tmp_path,operation):
    broker,manager,registration=controller
    values,output=arguments(operation,fixtures,tmp_path)
    manager.publication=output
    progress=[]
    p=PROFILES[operation]
    for name in p.callbacks:
        if name != 'cancellation_check': values[name]=lambda *args:progress.append(args)
    result=getattr(operations,operation)(**values)
    assert not registration.blocked
    assert broker.journal.snapshot()['slots'][0]['state']=='FREE'
    assert manager.cleaned
    if p.output:
        assert output.is_file() and output.stat().st_size>0
        if output.suffix=='.pdf':
            with pymupdf.open(output) as pdf:
                assert len(pdf)==1
                if operation in ('execute_ocr_markup','execute_studio_markup_region_ocr','execute_legacy_markup','process_markup_pdf'):assert list(pdf[0].annots()) or pdf[0].get_drawings()
        elif output.suffix=='.docx':
            import zipfile
            assert 'word/document.xml' in zipfile.ZipFile(output).namelist()
        else:assert 'F11' in output.read_text()
    if operation=='inspect_pdf':assert result==1
    if operation=='execute_ocr_text':assert 'F11 OPERATION FIXTURE' in result.pages[0].text
    if operation=='editor_colors':assert result['pages'][0]['elements'][0]['text_color']=='#000000'
    if operation in ('execute_ocr_text','execute_document_extraction'): assert progress


@pytest.mark.parametrize('operation',tuple(PROFILES))
def test_all24_reject_unknown_authority_fields_before_bridge(fixtures,tmp_path,operation):
    from app.controller.adapter import translate
    values,_=arguments(operation,fixtures,tmp_path)
    p=PROFILES[operation];root=tmp_path/'request';root.mkdir(mode=0o700);(root/'tmp').mkdir()
    if p.output:values[p.output]=str(root/'tmp'/('output'+Path(values[p.output]).suffix))
    values['command']='/bin/sh'
    boundary.write_frame(root/'request.json',{'operation':operation,'args':boundary.encode([]),'kwargs':boundary.encode(values),'callbacks':[],'raster_limit':None})
    launch=d.Launch((sys.executable,'-m','app.core.native_supervisor',str(root),str(os.getpid())),{},300,65536)
    with pytest.raises(d.DomainUnavailable):translate(launch)


def test_profiles_cover_exact_shared_registry():
    assert set(PROFILES)==set(operations.OPERATIONS)


def test_running_cancellation_drains_and_never_publishes(controller,fixtures,tmp_path):
    from app.jobs.cancellation import JobCancelledException
    broker,manager,registration=controller
    values,output=arguments('execute_searchable_pdf',fixtures,tmp_path)
    def cancel(*args): raise JobCancelledException('fixture cancellation')
    values['page_progress_callback']=cancel
    with pytest.raises(JobCancelledException):operations.execute_searchable_pdf(**values)
    assert not output.exists()
    assert all(job['child'].poll() is not None for job in manager.jobs.values())
    assert registration.blocked
    assert broker.journal.blocked()


def test_changed_drain_artifact_cannot_publish(controller,fixtures,tmp_path,monkeypatch):
    from app.core.ocr_v2.errors import EngineUnavailableError
    broker,manager,registration=controller
    original=manager.drain
    def changed(*args):
        result=dict(original(*args));result['artifacts']=dict(result['artifacts'])
        result['artifacts']['output.pdf']=b'digest-mismatch'
        return result
    monkeypatch.setattr(manager,'drain',changed)
    values,output=arguments('preview_source',fixtures,tmp_path)
    with pytest.raises(EngineUnavailableError):operations.preview_source(**values)
    assert not output.exists()
    assert registration.blocked and broker.journal.blocked()


def test_out_of_order_progress_is_refused_and_drained(controller,fixtures,tmp_path,monkeypatch):
    from app.core.ocr_v2.errors import EngineUnavailableError
    broker,manager,registration=controller
    original=manager.poll
    def changed(*args):
        result=original(*args)
        if isinstance(result,dict) and 'event' in result:
            result['event']['sequence']+=1
        return result
    monkeypatch.setattr(manager,'poll',changed)
    progress=[]
    with pytest.raises(EngineUnavailableError):
        operations.execute_ocr_text(fixtures[0],language='eng',routing_policy='FAST',page_progress_callback=lambda *args:progress.append(args))
    assert progress==[]
    assert all(job['child'].poll() is not None for job in manager.jobs.values())
    assert registration.blocked


@pytest.mark.parametrize('data',[b'{"result":NaN}',b'{"result":{"$":"record","n":"os.system","v":{}}}',b'{"result":null,"extra":"field"}',b'[]'])
def test_untrusted_output_frame_schema_refuses_nonfinite_or_import_data(data):
    from app.controller.profiles import validate_frame
    with pytest.raises(d.DomainUnavailable):validate_frame(data)

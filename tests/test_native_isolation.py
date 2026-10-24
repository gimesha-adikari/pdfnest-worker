import asyncio
import io
import os
from pathlib import Path
import sys
import time

import pymupdf as fitz
from PIL import Image, ImageDraw
import pytest

from app.core import native_isolation as boundary, native_operations as operations
from app.core.ocr_v2.errors import EngineUnavailableError, OCRTimeoutError, RasterAdmissionError
from app.core.ocr_v2.geometry import raster_policy
from app.jobs.cancellation import JobCancelledException


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / 'small.pdf'
    with fitz.open() as doc:
        for text in ('F11 FIRST PAGE', 'F11 SECOND PAGE'):
            page = doc.new_page(width=300, height=200)
            page.insert_text((20, 40), text, fontsize=15)
        doc.save(path)
    return path


@pytest.mark.parametrize('engine', ['internal', 'sdk'])
def test_real_text_structured_and_preview(engine, monkeypatch, pdf):
    for selector in ('OCR_TEXT_ENGINE', 'DOCUMENT_EXTRACTION_ENGINE', 'OCR_MARKUP_ENGINE', 'PDF_TO_MARKDOWN_ENGINE'):
        monkeypatch.setenv(selector, engine)
    progress = []
    result = operations.execute_ocr_text(pdf, language='eng', routing_policy='FAST',
        page_progress_callback=lambda done, total, page: progress.append((done, total, page.text)))
    assert 'FIRST PAGE' in result.pages[0].text
    assert 'SECOND PAGE' in result.pages[1].text
    assert [item[:2] for item in progress] == [(1, 2), (2, 2)]
    assert result.validation.valid
    structured = operations.execute_document_extraction(pdf, language='eng', routing_policy='FAST')
    assert len(structured.pages) == 2
    assert structured.to_dict()['pages'][0]['status'] == 'SUCCESS'
    preview = operations.execute_ocr_markup_preview(pdf, language='eng')
    assert len(preview['pages']) == 2
    markdown = operations.execute_pdf_to_markdown(pdf, language='eng', routing_policy='FAST')
    assert 'FIRST PAGE' in markdown.markdown


@pytest.mark.parametrize('engine', ['internal', 'sdk'])
def test_smaller_raster_policy_and_preallocation(engine, monkeypatch, tmp_path):
    monkeypatch.setenv('OCR_TEXT_ENGINE', engine)
    # An image scan forces rasterization; the actual input is tiny.
    image = Image.new('RGB', (400, 200), 'white')
    ImageDraw.Draw(image).text((20, 40), 'SAFE OCR', fill='black')
    stream = io.BytesIO(); image.save(stream, format='PNG')
    path = tmp_path / 'scan.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=200, height=100)
        page.insert_image(page.rect, stream=stream.getvalue())
        doc.save(path)
    with raster_policy(100):
        with pytest.raises(RasterAdmissionError):
            operations.execute_ocr_text(path, language='eng', routing_policy='FAST')
    result = operations.execute_ocr_text(path, language='eng', routing_policy='FAST')
    assert result.pages[0].processing_source.value == 'OCR_RECOGNITION'


@pytest.fixture
def child_probe(monkeypatch):
    original = boundary.run_hardened_subprocess
    def launch(cmd, **kwargs):
        code = (
            "from app.core import native_operations as o,native_isolation as b;"
            "o.OPERATIONS['inspect_pdf']=('tests.native_isolation_probes','probe',None);"
            "b.CHILD_CPU=1;"
            "from app.core.native_child import main;main()"
        )
        guard = 'from app.core.native_supervisor import main;import sys;sys.exit(main(' + repr([sys.executable, '-c', code, cmd[3]]) + '))'
        return original([sys.executable, '-c', guard, cmd[3], str(os.getpid())], **kwargs)
    monkeypatch.setattr(boundary, 'run_hardened_subprocess', launch)


def assert_dead(pid):
    # A killed grandchild can briefly remain an init-owned zombie, but cannot
    # run. The supervised direct child is always wait/reaped by the parent.
    status = Path(f'/proc/{pid}/stat')
    assert not status.exists() or status.read_text().split()[2] == 'Z'


def test_timeout_tree_cleanup_and_recovery(child_probe, monkeypatch, tmp_path, pdf):
    monkeypatch.setattr(boundary, 'CHILD_WALL', 3.0)
    with pytest.raises(OCRTimeoutError):
        operations.inspect_pdf(tmp_path, 'hang')
    assert_dead(int((tmp_path / 'child.pid').read_text()))
    assert_dead(int((tmp_path / 'grandchild.pid').read_text()))
    assert operations.inspect_pdf(tmp_path, 'valid')['pid'] != os.getpid()
    monkeypatch.setattr(boundary, 'CHILD_WALL', 300)
    assert 'FIRST PAGE' in operations.execute_ocr_text(pdf, language='eng', routing_policy='FAST').pages[0].text


@pytest.mark.parametrize('mode', ['memory', 'cpu', 'crash'])
def test_resource_failures_parent_survives_and_recovers(child_probe, tmp_path, mode, pdf):
    with pytest.raises(EngineUnavailableError):
        operations.inspect_pdf(tmp_path, mode)
    assert_dead(int((tmp_path / 'child.pid').read_text()))
    assert operations.inspect_pdf(tmp_path, 'valid')['memory'] == 1024 * 1024 * 1024
    assert 'FIRST PAGE' in operations.execute_ocr_text(pdf, language='eng', routing_policy='FAST').pages[0].text


def test_output_capture_is_bounded(child_probe, tmp_path):
    with pytest.raises(RuntimeError, match='stdout exceeded'):
        operations.inspect_pdf(tmp_path, 'flood')
    assert_dead(int((tmp_path / 'child.pid').read_text()))


def test_cancel_kills_tree_and_preserves_callback_exception(child_probe, tmp_path, monkeypatch):
    temporary_root = tmp_path / 'temporary'
    temporary_root.mkdir()
    monkeypatch.setattr(boundary.tempfile, 'tempdir', str(temporary_root))
    def cancel():
        if (tmp_path / 'grandchild.pid').exists():
            raise JobCancelledException('synthetic cancellation')
    with pytest.raises(JobCancelledException, match='synthetic'):
        operations.inspect_pdf(tmp_path, 'hang', cancellation_check=cancel)
    assert_dead(int((tmp_path / 'child.pid').read_text()))
    assert_dead(int((tmp_path / 'grandchild.pid').read_text()))

    assert all(path.name.startswith('platen-native-slots-') for path in temporary_root.iterdir())


def test_callback_rejection_prevents_output(pdf, tmp_path):
    output = tmp_path / 'result.pdf'
    output.write_bytes(b'preexisting')
    def reject(result):
        raise JobCancelledException('reject before render')
    with pytest.raises(JobCancelledException):
        operations.execute_searchable_pdf(pdf, output, language='eng', after_ocr=reject)
    assert output.read_bytes() == b'preexisting'


def test_protocol_limits_and_no_arbitrary_import(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, 'MAX_FRAME', 100)
    with pytest.raises(ValueError, match='frame limit'):
        boundary.write_frame(tmp_path / 'frame', {'text': 'x' * 200})
    (tmp_path / 'frame').write_bytes(b'x' * 101)
    with pytest.raises(ValueError, match='frame limit'):
        boundary.read_frame(tmp_path / 'frame')
    with pytest.raises(KeyError):
        boundary.decode({'$':'record', 'n':'os.system', 'v':{}})
    with pytest.raises(ValueError, match='Unsupported isolated'):
        boundary.execute('os.system')


@pytest.mark.anyio
async def test_async_cancellation_joins_child_before_cleanup(child_probe, tmp_path):
    task = asyncio.create_task(boundary.execute_async('inspect_pdf', tmp_path, 'hang'))
    deadline = time.monotonic() + 5
    while not (tmp_path / 'grandchild.pid').exists():
        assert time.monotonic() < deadline
        await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_dead(int((tmp_path / 'child.pid').read_text()))
    assert_dead(int((tmp_path / 'grandchild.pid').read_text()))


@pytest.mark.parametrize('engine', ['internal', 'sdk'])
def test_scanned_searchable_word_and_markup(engine, monkeypatch, tmp_path):
    from PIL import ImageFont
    from docx import Document
    for selector in ('SEARCHABLE_PDF_ENGINE', 'PDF_TO_WORD_OCR_ENGINE', 'OCR_MARKUP_ENGINE', 'EDITOR_OCR_ENGINE', 'STUDIO_EDITOR_EXTRACTION_ENGINE', 'LEGACY_EDITOR_OCR_ENGINE'):
        monkeypatch.setenv(selector, engine)
    image = Image.new('RGB', (600, 400), 'white')
    ImageDraw.Draw(image).text((40, 80), 'SAFE OCR DOCUMENT', font=ImageFont.truetype('DejaVuSans.ttf', 30), fill='black')
    image_path = tmp_path / 'image.png'; image.save(image_path)
    source = tmp_path / 'source.pdf'
    metadata = operations.image_source_metadata([image_path], source)
    assert metadata[0]['width'] == 600
    assert 'png_bytes' not in metadata[0]
    output = tmp_path / 'searchable.pdf'
    result = operations.execute_searchable_pdf(source, output, language='eng')
    assert result.validation.valid
    with fitz.open(output) as document:
        assert len(document) == 1
        assert document[0].get_images()
        assert 'SAFE OCR DOCUMENT' in ' '.join(document[0].get_text().split())
    word = tmp_path / 'output.docx'
    operations.convert_to_word(source, word, cancellation_check=lambda: None)
    assert 'SAFE OCR DOCUMENT' in ' '.join(' '.join(p.text for p in Document(word).paragraphs).split())
    marked = tmp_path / 'marked.pdf'
    operations.execute_ocr_markup(source, marked, action='highlight', query='SAFE', language='eng')
    with fitz.open(marked) as document:
        assert document[0].get_images()
        assert list(document[0].annots())
    for operation in (operations.execute_editor_ocr, operations.execute_studio_editor_extraction):
        layout = operation(source, language_mode='EXPLICIT', languages=['eng'])
        assert layout['pages']
    assert operations.execute_legacy_editor_ocr(source)['pages']


def test_capacity_is_shared_across_processes_and_cancellable(tmp_path):
    import signal
    import subprocess
    marker = tmp_path / 'capacity-ready'
    code = (
        'from pathlib import Path;import time;from app.core.native_isolation import _capacity;'
        'lock=_capacity(lambda:None,2);lock.__enter__();'
        f'Path({str(marker)!r}).touch();time.sleep(30)'
    )
    holder = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert holder.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls > 3:
                raise JobCancelledException('capacity cancellation')
        with pytest.raises(JobCancelledException):
            with boundary._capacity(check, 2):
                pytest.fail('Second process bypassed document capacity')
    finally:
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()
    with boundary._capacity(lambda: None, .2):
        pass


def test_legacy_text_editor_compile_and_native_colors(pdf, tmp_path):
    import json
    text_path = tmp_path / 'ocr.txt'
    operations.extract_text_from_pdf(pdf, text_path, lang='eng')
    assert 'FIRST PAGE' in text_path.read_text()
    layout = operations.extract_document_v2(pdf)
    colored = operations.editor_colors(layout, str(pdf))
    assert colored['pages'][0]['elements']
    layout_path = tmp_path / 'layout.json'
    layout_path.write_text(json.dumps(colored))
    output = tmp_path / 'compiled.pdf'
    operations.compile_document(str(pdf), str(output), str(layout_path))
    with fitz.open(output) as document:
        assert len(document) == 2
        assert 'FIRST PAGE' in document[0].get_text()


@pytest.mark.parametrize('engine', ['internal', 'sdk'])
def test_legacy_and_studio_markup_outputs(engine, monkeypatch, pdf, tmp_path):
    monkeypatch.setenv('LEGACY_MARKUP_OCR_ENGINE', engine)
    monkeypatch.setenv('STUDIO_MARKUP_REGION_OCR_ENGINE', engine)
    boxes = [{'page': 1, 'x': 15, 'y': 20, 'width': 250, 'height': 30}]
    for operation in (operations.execute_legacy_markup, operations.execute_studio_markup_region_ocr):
        output = tmp_path / (operation.args[0] + '.pdf')
        operation(pdf, output, boxes=boxes, action='highlight', mode='smart')
        with fitz.open(output) as document:
            assert len(document) == 2
            assert 'SECOND PAGE' in document[1].get_text()
            assert list(document[0].annots()) or document[0].get_drawings()


def test_unrelated_sdk_selector_does_not_require_sdk(monkeypatch, pdf):
    original = boundary.run_hardened_subprocess
    def launch(cmd, **kwargs):
        code = '''
import importlib.abc,sys
class NoSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('platen_document'):
            raise ModuleNotFoundError('SDK deliberately unavailable', name=fullname)
sys.meta_path.insert(0,NoSDK())
from app.core.native_child import main
main()
'''
        return original([sys.executable, '-c', code, cmd[3]], **kwargs)
    monkeypatch.setattr(boundary, 'run_hardened_subprocess', launch)
    monkeypatch.setenv('SEARCHABLE_PDF_ENGINE', 'sdk')
    monkeypatch.setenv('OCR_TEXT_ENGINE', 'internal')
    assert operations.inspect_pdf(pdf, 10) == 2
    assert 'FIRST PAGE' in operations.execute_ocr_text(pdf, language='eng', routing_policy='FAST').pages[0].text
    monkeypatch.setenv('OCR_TEXT_ENGINE', 'sdk')
    from app.core.ocr_text_engine import OCRTextEngineUnavailableError
    with pytest.raises(OCRTextEngineUnavailableError):
        operations.execute_ocr_text(pdf, language='eng', routing_policy='FAST')


@pytest.mark.anyio
@pytest.mark.parametrize('endpoint', ['text', 'markup/preview'])
@pytest.mark.parametrize('error,status,code', [(OCRTimeoutError,504,'TIMEOUT'), (EngineUnavailableError,503,'ENGINE_UNAVAILABLE'), (JobCancelledException,499,'CANCELLED')])
async def test_inspection_errors_are_mapped_and_cleaned(endpoint, error, status, code, monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from app.api.ocr_v2 import router as module
    cleanups = []
    monkeypatch.setattr(module, 'create_route_cancellation_checker', lambda request: (lambda: None, lambda: cleanups.append(True)))
    original = module.tempfile.mkstemp
    monkeypatch.setattr(module.tempfile, 'mkstemp', lambda **kw: original(dir=tmp_path, **kw))
    async def fail(*args, **kwargs):
        raise error('synthetic failure')
    monkeypatch.setattr(module, 'execute_async', fail)
    app = FastAPI(); app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/internal/ocr/v2/' + endpoint,
            files={'file':('input.pdf',b'%PDF-fake','application/pdf')},
            data={'request_id':'isolated-stage','language':'eng','profile':'OCR_TEXT_V2' if endpoint == 'text' else 'MARKUP_V2'})
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert not list(tmp_path.iterdir())
    assert cleanups == [True]


@pytest.mark.anyio
@pytest.mark.parametrize('error,status,code', [(OCRTimeoutError,504,'TIMEOUT'), (EngineUnavailableError,503,'ENGINE_UNAVAILABLE')])
async def test_text_operation_resource_errors_are_mapped(error, status, code, monkeypatch, tmp_path):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.ocr_v2 import router as module
    path = tmp_path / 'staged.pdf'; path.write_bytes(b'%PDF-fake')
    async def stage(*args): return str(path)
    async def fail(*args): raise error('synthetic')
    monkeypatch.setattr(module, '_stage_pdf', stage)
    monkeypatch.setattr(module, '_run_worker', fail)
    monkeypatch.setattr(module, 'create_route_cancellation_checker', lambda request: (lambda: None, lambda: None))
    monkeypatch.setattr(module.PPOCRv6MediumAdapter,'availability',lambda self:SimpleNamespace(available=True))
    monkeypatch.setattr(module.TesseractAdapter,'availability',lambda self:SimpleNamespace(available=True))
    app = FastAPI(); app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/internal/ocr/v2/text', files={'file':('input.pdf',b'%PDF-fake','application/pdf')},data={'request_id':'isolated-run','language':'eng'})
    assert response.status_code == status
    assert response.json()['code'] == code
    assert not path.exists()


def test_crashed_child_with_inherited_pipes_stops_descendants(child_probe, tmp_path):
    started = time.monotonic()
    with pytest.raises(EngineUnavailableError):
        operations.inspect_pdf(tmp_path, 'orphan_crash')
    assert time.monotonic() - started < 5
    assert_dead(int((tmp_path / 'grandchild.pid').read_text()))


def test_owner_death_stops_native_tree(tmp_path):
    import signal
    import subprocess
    import shutil
    script = '''
import os,sys
from pathlib import Path
from app.core import native_isolation as b
original=b.run_hardened_subprocess
probe="from app.core import native_operations as o;o.OPERATIONS['inspect_pdf']=('tests.native_isolation_probes','probe',None);from app.core.native_child import main;main()"
def launch(cmd,**kwargs):
    Path(sys.argv[1],'owned-root').write_text(cmd[3])
    guard='from app.core.native_supervisor import main;import sys;sys.exit(main('+repr([sys.executable,'-c',probe,cmd[3]])+'))'
    return original([sys.executable,'-c',guard,cmd[3],str(os.getpid())],**kwargs)
b.run_hardened_subprocess=launch
b.execute('inspect_pdf',sys.argv[1],'hang')
'''
    owner = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], start_new_session=True)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'grandchild.pid').exists():
            assert owner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        owner.kill(); owner.wait()
        deadline = time.monotonic() + 3
        while True:
            pids = [int((tmp_path / name).read_text()) for name in ('child.pid', 'grandchild.pid')]
            try:
                for pid in pids: assert_dead(pid)
                break
            except AssertionError:
                assert time.monotonic() < deadline
                time.sleep(.05)
    finally:
        if owner.poll() is None:
            owner.kill(); owner.wait()
        root_file = tmp_path / 'owned-root'
        if root_file.exists():
            shutil.rmtree(root_file.read_text(), ignore_errors=True)


def test_office_word_cancel_preserves_cleanup(monkeypatch, tmp_path):
    from fastapi import HTTPException, UploadFile
    from types import SimpleNamespace
    from app.api.tools.pdf_to_office import service
    source, output = tmp_path / 'source.pdf', tmp_path / 'word.docx'
    monkeypatch.setattr(service, 'create_temp_paths', lambda suffix: SimpleNamespace(input_path=str(source),output_path=str(output)))
    def cancel(): raise JobCancelledException('synthetic disconnect')
    with pytest.raises(HTTPException) as exc:
        service.OfficeConversionService._convert_sync('docx', UploadFile(file=io.BytesIO(b'%PDF-synthetic'),filename='source.pdf'), 'eng', cancel)
    assert exc.value.status_code == 499
    assert not source.exists() and not output.exists()


@pytest.mark.anyio
async def test_markdown_cancel_preserves_cleanup(monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from app.api.tools.markdown import router as module
    source = tmp_path / 'source.pdf'
    cleanups = []
    def cancel(): raise JobCancelledException('synthetic disconnect')
    monkeypatch.setattr(module, 'create_route_cancellation_checker', lambda request: (cancel,lambda: cleanups.append(True)))
    monkeypatch.setattr(module, 'temp_file_path', lambda *args: str(source))
    app = FastAPI(); app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/v1/markdown/convert',files={'file':('source.pdf',b'%PDF-synthetic','application/pdf')})
    assert response.status_code == 499
    assert cleanups == [True]
    assert not source.exists()

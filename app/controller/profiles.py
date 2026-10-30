"""Source-fixed native operation schemas. No module, command or environment authority.

This module is deliberately standard-library-only: broker and root manager can
validate a request without importing a PDF parser or the application engines.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass
from app.core.native_domain import DomainUnavailable

VERSION = 'native-operations-v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 12 * 1024 * 1024
MAX_EVENTS = 1000
ROOT = '/work/job'
CALLBACKS = frozenset(('cancellation_check', 'page_progress_callback', 'progress_callback', 'progress_cb', 'after_ocr'))
LANGUAGE = 'language language_mode languages language_usage routing_policy'

@dataclass(frozen=True)
class Profile:
    parameters: tuple[str, ...]
    required: tuple[str, ...]
    inputs: tuple[str, ...]
    output: str | None = None
    sequences: tuple[str, ...] = ()
    callbacks: tuple[str, ...] = ('cancellation_check',)

def profile(parameters, required, inputs, output=None, sequences=(), callbacks=('cancellation_check',)):
    return Profile(tuple(parameters.split()), tuple(required.split()), tuple(inputs.split()), output, sequences, callbacks)

PROFILES = {}
for name in ('execute_ocr_text', 'execute_document_extraction', 'execute_pdf_to_markdown'):
    PROFILES[name] = profile('pdf_path ' + LANGUAGE + (' page_timeout_seconds' if name == 'execute_ocr_text' else ''),
                             'pdf_path language', 'pdf_path', callbacks=('cancellation_check', 'page_progress_callback'))
PROFILES.update({
 'execute_searchable_pdf': profile('source_pdf output_pdf ' + LANGUAGE.replace(' routing_policy', '') + ' diagnostic_job_id', 'source_pdf output_pdf language', 'source_pdf', 'output_pdf', callbacks=('cancellation_check','page_progress_callback','after_ocr')),
 'execute_ocr_markup': profile('input_path output_path action query selection ' + LANGUAGE.replace(' routing_policy','') + ' mode color', 'input_path output_path action', 'input_path', 'output_path', callbacks=('cancellation_check','progress_callback')),
 'execute_ocr_markup_preview': profile('input_path ' + LANGUAGE.replace(' routing_policy',''), 'input_path', 'input_path', callbacks=('cancellation_check','progress_callback')),
 'execute_editor_ocr': profile('input_path password language_mode languages', 'input_path', 'input_path', callbacks=('cancellation_check','page_progress_callback')),
 'execute_legacy_editor_ocr': profile('input_path password', 'input_path', 'input_path', callbacks=('cancellation_check','page_progress_callback')),
 'execute_studio_editor_extraction': profile('input_path password language_mode languages', 'input_path', 'input_path', callbacks=('cancellation_check','page_progress_callback')),
 'execute_studio_markup_region_ocr': profile('input_path output_path boxes action mode password', 'input_path output_path boxes action', 'input_path', 'output_path', callbacks=('cancellation_check','progress_callback')),
 'execute_legacy_markup': profile('input_path output_path boxes action mode password', 'input_path output_path boxes action', 'input_path', 'output_path', callbacks=('cancellation_check','progress_callback')),
 'extract_text_from_pdf': profile('input_path output_path lang password', 'input_path output_path', 'input_path', 'output_path'),
 'build_searchable_pdf_from_images': profile('image_paths output_path lang', 'image_paths output_path', 'image_paths', 'output_path', ('image_paths',)),
 'extract_document': profile('input_path password', 'input_path', 'input_path'),
 'extract_document_v2': profile('input_path password language_mode languages native_geometry_mode', 'input_path', 'input_path', callbacks=('cancellation_check','page_progress_callback')),
 'compile_document': profile('original_pdf_path output_pdf_path pages_json_path', 'original_pdf_path output_pdf_path pages_json_path', 'original_pdf_path pages_json_path', 'output_pdf_path'),
 'process_markup_pdf': profile('input_path output_path boxes action mode password canonical_boxes', 'input_path output_path boxes action', 'input_path', 'output_path', callbacks=('progress_callback',)),
 'convert_pdf_to_markdown': profile('input_path password lang include_annotations embed_images', 'input_path', 'input_path', callbacks=('cancellation_check','progress_cb')),
 'convert_to_word': profile('pdf_path output_path language', 'pdf_path output_path', 'pdf_path', 'output_path', callbacks=()),
 'image_source_metadata': profile('image_paths output_pdf dpi', 'image_paths output_pdf', 'image_paths', 'output_pdf', ('image_paths',), callbacks=()),
 'editor_colors': profile('layout source_path password visible_geometry', 'layout source_path', 'source_path'),
 'inspect_pdf': profile('path max_pages', 'path max_pages', 'path'),
 'preview_source': profile('input_path output_path page_index', 'input_path output_path page_index', 'input_path', 'output_path'),
 'structured_document': profile('input_path ' + LANGUAGE, 'input_path', 'input_path', callbacks=('cancellation_check','page_progress_callback')),
})

def plain(value, depth=0):
    """Validate the existing IPC value algebra without importing supplied types."""
    if depth > 40:
        raise DomainUnavailable()
    if value is None or type(value) in (bool, int, str):
        if type(value) is str and (len(value) > 1024 * 1024 or '\x00' in value):
            raise DomainUnavailable()
        if type(value) is int and abs(value) >= 2**63:
            raise DomainUnavailable()
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is not dict:
        raise DomainUnavailable()
    kind = value.get('$')
    if kind == 'enum' and set(value) == {'$', 'n', 'v'}:
        if not isinstance(value['n'], str) or value['n'] not in (
            'app.core.ocr_v2.markup.MarkupAction', 'app.core.ocr_v2.markup.MarkupMode',
            'app.core.ocr_v2.validation.OCRProfile', 'app.core.ocr_v2.routing.RoutePolicy',
            'app.core.ocr_v2.native.NativeGeometryMode',
            'platen_document.engine.markup.MarkupAction', 'platen_document.engine.markup.MarkupMode'):
            raise DomainUnavailable()
        return plain(value['v'], depth + 1)
    if set(value) != {'$', 'v'} or type(value['v']) is not list or len(value['v']) > 100000:
        raise DomainUnavailable()
    if kind in ('list', 'tuple'):
        return [plain(x, depth + 1) for x in value['v']]
    if kind == 'map':
        result = {}
        for pair in value['v']:
            if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or pair[0] in result:
                raise DomainUnavailable()
            result[pair[0]] = plain(pair[1], depth + 1)
        return result
    raise DomainUnavailable()

def encoded(value):
    if type(value) is dict:
        return {'$': 'map', 'v': [[k, encoded(v)] for k,v in value.items()]}
    if type(value) is list:
        return {'$': 'list', 'v': [encoded(v) for v in value]}
    return value

def options_valid(values, p):
    if type(values) is not dict: raise DomainUnavailable()
    if set(values) - set(p.parameters) or set(p.required) - set(values):
        raise DomainUnavailable()
    for key, value in values.items():
        if key in p.inputs or key == p.output:
            continue
        if key in ('boxes', 'languages', 'color'):
            if value is not None and type(value) is not list:
                raise DomainUnavailable()
            if key == 'languages' and value is not None and (len(value)>128 or any(type(v) is not str or not re.fullmatch(r'[A-Za-z0-9_+-]{1,128}',v) for v in value)):
                raise DomainUnavailable()
            if key == 'boxes' and (len(value)>4096 or any(type(v) is not dict for v in value)):
                raise DomainUnavailable()
            if key == 'color' and (len(value)!=3 or any(type(v) not in (int,float) or not 0<=v<=1 for v in value)):
                raise DomainUnavailable()
        elif key in ('layout', 'selection', 'language_usage'):
            if value is not None and type(value) is not dict:
                raise DomainUnavailable()
        elif key in ('page_index','max_pages','dpi'):
            if type(value) is not int or not (0 if key == 'page_index' else 1) <= value <= (1200 if key == 'dpi' else 10000):
                raise DomainUnavailable()
        elif key == 'action':
            if value not in ('highlight','underline','strikeout'): raise DomainUnavailable()
        elif key == 'mode':
            if value not in ('smart','ocr','native','manual','text'): raise DomainUnavailable()
        elif key == 'page_timeout_seconds':
            if value is not None and (type(value) not in (int,float) or not 0 < value <= 300):
                raise DomainUnavailable()
        elif key in ('visible_geometry','canonical_boxes','include_annotations','embed_images'):
            if type(value) is not bool:
                raise DomainUnavailable()
        elif value is not None and (type(value) is not str or len(value) > (65536 if key == 'query' else 1024)):
            raise DomainUnavailable()


def snapshot(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1 or not 0 < before.st_size <= MAX_BYTES:
            raise DomainUnavailable()
        data = bytearray()
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        after = os.fstat(fd)
        if any(getattr(before,k) != getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or len(data) != before.st_size:
            raise DomainUnavailable()
        return bytes(data)
    except (OSError,ValueError,TypeError) as exc:
        raise DomainUnavailable() from exc
    finally:
        if fd is not None: os.close(fd)


def translate_request(root):
    root = Path(root)
    info = root.lstat()
    if root != root.resolve() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DomainUnavailable()
    request = json.loads(snapshot(root / 'request.json'))
    if type(request) is not dict or set(request) != {'operation','args','kwargs','callbacks','raster_limit'}:
        raise DomainUnavailable()
    p = PROFILES.get(request['operation'])
    if p is None: raise DomainUnavailable()
    args, kwargs = plain(request['args']), plain(request['kwargs'])
    if type(args) is not list or type(kwargs) is not dict or len(args) > len(p.parameters):
        raise DomainUnavailable()
    values = dict(zip(p.parameters, args))
    if values.keys() & kwargs.keys(): raise DomainUnavailable()
    values.update(kwargs)
    options_valid(values,p)
    callbacks = request['callbacks']
    if type(callbacks) is not list or len(set(callbacks)) != len(callbacks) or any(x not in p.callbacks for x in callbacks):
        raise DomainUnavailable()
    raster = request['raster_limit']
    if raster is not None and (type(raster) is not int or not 0 < raster <= 100000000):
        raise DomainUnavailable()
    files = {}
    for name in p.inputs:
        paths = values[name] if name in p.sequences else [values[name]]
        if type(paths) is not list or not 0 < len(paths) <= 128:
            raise DomainUnavailable()
        replaced = []
        for path in paths:
            if type(path) is not str or len(path) > 4096: raise DomainUnavailable()
            suffix = Path(path).suffix.lower()
            if suffix not in ('.pdf','.png','.jpg','.jpeg','.webp','.tif','.tiff','.bmp','.json'): raise DomainUnavailable()
            target = 'input-' + str(len(files)).zfill(3) + suffix
            files[target] = snapshot(path)
            replaced.append(ROOT + '/' + target)
        values[name] = replaced if name in p.sequences else replaced[0]
    if p.output:
        path = values[p.output]
        if type(path) is not str or Path(path).parent != root / 'tmp' or Path(path).name not in ('output.pdf','output.txt','output.docx'):
            raise DomainUnavailable()
        values[p.output] = ROOT + '/' + Path(path).name
    canonical = {'operation':request['operation'],'args':encoded([]),'kwargs':encoded(values),
                 'callbacks':callbacks,'raster_limit':raster}
    manifest = {'kind':'native_operation', 'profile':VERSION,'request':json.dumps(canonical,allow_nan=False,separators=(',',':')).encode(), 'files':files}
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest):
    if type(manifest) is not dict: raise DomainUnavailable()
    if manifest.get('kind') == 'render_metadata':
        if set(manifest) != {'kind','input'} or type(manifest['input']) is not bytes or not 0 < len(manifest['input']) <= MAX_BYTES:
            raise DomainUnavailable()
        return
    if set(manifest) != {'kind','profile','request','files'} or manifest['kind'] != 'native_operation' or manifest['profile'] != VERSION:
        raise DomainUnavailable()
    data, files = manifest['request'], manifest['files']
    if type(data) is not bytes or type(files) is not dict or not 0 < len(files) <= 128:
        raise DomainUnavailable()
    if len(data) + sum(len(x) if type(x) is bytes else MAX_BYTES+1 for x in files.values()) > MAX_BYTES:
        raise DomainUnavailable()
    if any(type(k) is not str or re.fullmatch(r'input-[0-9]{3}\.(pdf|png|jpg|jpeg|webp|tif|tiff|bmp|json)', k) is None or type(v) is not bytes or not v for k,v in files.items()):
        raise DomainUnavailable()
    try: req=json.loads(data)
    except (ValueError,UnicodeError,RecursionError) as exc: raise DomainUnavailable() from exc
    if type(req) is not dict or set(req) != {'operation','args','kwargs','callbacks','raster_limit'} or req['operation'] not in PROFILES or plain(req['args']) != []:
        raise DomainUnavailable()
    p=PROFILES[req['operation']]; values=plain(req['kwargs']); options_valid(values,p)
    used=[]
    for name in p.inputs:
        paths=values[name] if name in p.sequences else [values[name]]
        if type(paths) is not list: raise DomainUnavailable()
        for path in paths:
            if type(path) is not str or not path.startswith(ROOT+'/') or path[len(ROOT)+1:] not in files: raise DomainUnavailable()
            used.append(path[len(ROOT)+1:])
    if set(used) != set(files) or len(used) != len(files): raise DomainUnavailable()
    if p.output and values[p.output] not in (ROOT+'/output.pdf', ROOT+'/output.txt', ROOT+'/output.docx'): raise DomainUnavailable()
    if type(req['callbacks']) is not list or len(req['callbacks']) != len(set(req['callbacks'])) or any(c not in p.callbacks for c in req['callbacks']): raise DomainUnavailable()
    raster=req['raster_limit']
    if raster is not None and (type(raster) is not int or not 0 < raster <= 100000000): raise DomainUnavailable()


def result_digest(result):
    from app.controller.transport import pack
    return hashlib.sha256(json.dumps(pack(result),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_result(result):
    if type(result) is not dict or set(result) not in ({'returncode','stdout','stderr','reaped'}, {'returncode','stdout','stderr','reaped','artifacts'}): raise DomainUnavailable()
    if result['reaped'] is not True or type(result['returncode']) is not int or any(type(result[k]) is not str or len(result[k].encode()) > (65536 if 'artifacts' in result else 1024) for k in ('stdout','stderr')): raise DomainUnavailable()
    if 'artifacts' in result:
        artifacts=result['artifacts']
        if type(artifacts) is not dict or set(artifacts)-{'result.json','output.pdf','output.txt','output.docx'} or 'result.json' not in artifacts or any(type(v) is not bytes for v in artifacts.values()) or sum(map(len,artifacts.values())) > MAX_ARTIFACT_BYTES: raise DomainUnavailable()
        validate_frame(artifacts['result.json'])


def validate_frame(data):
    """Validate untrusted result structure without importing a native type."""
    def bad_constant(_): raise DomainUnavailable()
    try: frame=json.loads(data,parse_constant=bad_constant)
    except (ValueError,UnicodeError,RecursionError) as exc: raise DomainUnavailable() from exc
    if type(frame) is not dict or set(frame) not in ({'result'},{'error'}): raise DomainUnavailable()
    def value(item,depth=0):
        if depth>60: raise DomainUnavailable()
        if item is None or type(item) in (bool,str,int): return
        if type(item) is float and math.isfinite(item): return
        if type(item) is not dict: raise DomainUnavailable()
        kind=item.get('$'); fields={'$','v','n'} if kind in ('enum','record') else {'$','v'}
        if set(item)!=fields: raise DomainUnavailable()
        data=item['v']
        if kind in ('enum','record'):
            prefixes=('app.core.ocr_v2.contracts.','app.core.ocr_v2.structured.','app.core.ocr_v2.markup.','app.core.ocr_v2.validation.','app.core.pdf_to_markdown_engine.','app.core.ocr_markup_engine.','platen_document.contracts.','platen_document.engine.contracts.','platen_document.engine.structured.','platen_document.engine.markup.')
            if type(item['n']) is not str or not item['n'].startswith(prefixes): raise DomainUnavailable()
            if kind=='enum':
                if type(data) not in (str,int,bool): raise DomainUnavailable()
            else:
                if type(data) is not dict: raise DomainUnavailable()
                for child in data.values(): value(child,depth+1)
        elif kind in ('list','tuple','set','frozenset'):
            if type(data) is not list: raise DomainUnavailable()
            for child in data:value(child,depth+1)
        elif kind=='map':
            if type(data) is not list: raise DomainUnavailable()
            for pair in data:
                if type(pair) is not list or len(pair)!=2: raise DomainUnavailable()
                for child in pair:value(child,depth+1)
        else: raise DomainUnavailable()
    if 'result' in frame:value(frame['result'])
    elif type(frame['error']) is not dict or len(json.dumps(frame['error']).encode())>65536:raise DomainUnavailable()
    return frame

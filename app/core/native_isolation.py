"""Bounded exec-based OCR supervision. No PDF parsing or pickle in the parent."""
from __future__ import annotations

import asyncio
import threading
import dataclasses
import enum
import fcntl
import importlib
import json
import os
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from app.core.subprocess_runner import run_hardened_subprocess

# Mandatory ceilings; configuration can only tighten them.
MAX_FRAME = 16 * 1024 * 1024
MAX_EVENTS = 1000
CHILD_MEMORY = 1024 * 1024 * 1024
CHILD_CPU = 180
CHILD_WALL = 300
CHILD_FILES = 64 * 1024 * 1024
IN_CHILD = False

_TYPE_MODULES = (
    'app.core.ocr_v2.contracts', 'app.core.ocr_v2.structured',
    'app.core.ocr_v2.markup', 'app.core.ocr_v2.validation',
    'app.core.pdf_to_markdown_engine', 'app.core.ocr_markup_engine',
    'platen_document.contracts', 'platen_document.engine.contracts',
    'platen_document.engine.structured', 'platen_document.engine.markup',
)
_ERROR_MODULES = (
    'app.core.ocr_v2.errors', 'app.core.ocr_text_engine',
    'app.core.searchable_pdf_engine', 'app.core.document_extraction_engine',
    'app.core.pdf_to_markdown_engine', 'app.core.ocr_markup_engine',
    'app.core.editor_ocr_engine', 'app.core.editor_language',
    'app.core.legacy_editor_ocr_engine', 'app.core.legacy_markup_ocr_engine',
    'app.core.studio_editor_extraction_engine', 'app.core.studio_markup_region_ocr_engine',
    'app.core.pdf_to_word_ocr_engine',
)


def _types():
    result = {}
    for module_name in _TYPE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or module_name.startswith(exc.name + '.'):
                continue
            raise
        for name, cls in vars(module).items():
            if isinstance(cls, type) and cls.__module__ == module_name and (
                dataclasses.is_dataclass(cls) or issubclass(cls, enum.Enum)
            ):
                result[module_name + '.' + name] = cls
    return result


def encode(value, depth=0):
    if depth > 60:
        raise ValueError('Native IPC nesting limit exceeded')
    child = lambda item: encode(item, depth + 1)
    if isinstance(value, enum.Enum):
        return {'$': 'enum', 'n': type(value).__module__ + '.' + type(value).__name__, 'v': value.value}
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {'$': 'record', 'n': type(value).__module__ + '.' + type(value).__name__,
                'v': {field.name: child(getattr(value, field.name)) for field in dataclasses.fields(value)}}
    if isinstance(value, dict):
        return {'$': 'map', 'v': [[child(key), child(item)] for key, item in value.items()]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {'$': type(value).__name__, 'v': [child(item) for item in value]}
    raise ValueError('Unsupported native IPC value')


def decode(value, types=None, depth=0):
    if depth > 60:
        raise ValueError('Native IPC nesting limit exceeded')
    if value is None or type(value) in (bool, int, float, str):
        return value
    if not isinstance(value, dict):
        raise ValueError('Invalid native IPC value')
    types = _types() if types is None else types
    child = lambda item: decode(item, types, depth + 1)
    kind = value['$']
    data = value['v']
    if kind in ('enum', 'record'):
        cls = types[value['n']]  # Never import a child-supplied module or unpickle.
        if kind == 'enum' and issubclass(cls, enum.Enum):
            return cls(data)
        if kind == 'record' and dataclasses.is_dataclass(cls):
            return cls(**{key: child(item) for key, item in data.items()})
        raise ValueError('Invalid native IPC type')
    if kind == 'map':
        return {child(key): child(item) for key, item in data}
    constructors = {'list': list, 'tuple': tuple, 'set': set, 'frozenset': frozenset}
    return constructors[kind](child(item) for item in data)


def write_frame(path: Path, value):
    staging = path.with_suffix('.writing')
    count = 0
    with staging.open('w', encoding='utf-8') as output:
        for chunk in json.JSONEncoder(allow_nan=False, ensure_ascii=True).iterencode(value):
            count += len(chunk)  # ensure_ascii makes the encoded length exact.
            if count > MAX_FRAME:
                raise ValueError('Native IPC frame limit exceeded')
            output.write(chunk)
    os.replace(staging, path)


def read_frame(path: Path):
    with path.open('rb') as source:
        data = source.read(MAX_FRAME + 1)
    if len(data) > MAX_FRAME:
        raise ValueError('Native IPC frame limit exceeded')
    return json.loads(data)


def error_frame(exc):
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return {'http': exc.status_code, 'detail': exc.detail, 'headers': exc.headers}
    return {'name': type(exc).__module__ + '.' + type(exc).__name__, 'message': str(exc)[:1024],
            'substage': getattr(exc, 'substage', None), 'reason_code': getattr(exc, 'reason_code', None)}


def raise_error(frame):
    from fastapi import HTTPException
    from app.core.ocr_v2.errors import EngineUnavailableError, RenderingNotEligibleError
    if 'http' in frame and type(frame['http']) is int and 400 <= frame['http'] <= 599:
        raise HTTPException(frame['http'], frame['detail'], headers=frame.get('headers'))
    import pymupdf
    allowed = {'builtins.ValueError': ValueError, 'builtins.FileNotFoundError': FileNotFoundError}
    for cls in (pymupdf.FileDataError, pymupdf.EmptyFileError):
        allowed[cls.__module__ + '.' + cls.__name__] = cls
    for module_name in _ERROR_MODULES:
        for name, cls in vars(importlib.import_module(module_name)).items():
            if isinstance(cls, type) and cls.__module__ == module_name and issubclass(cls, Exception):
                allowed[module_name + '.' + name] = cls
    cls = allowed.get(frame.get('name'))
    if cls is RenderingNotEligibleError:
        raise cls(frame['message'], substage=frame.get('substage'), reason_code=frame.get('reason_code'))
    if cls is not None:
        raise cls(frame['message'])
    raise EngineUnavailableError('Isolated native processing failed')


@contextmanager
def _capacity(check, timeout):
    # Shared by local API/actor replicas of the same uid and filesystem. This
    # is not a cross-container/node aggregate resource limit.
    root = Path(tempfile.gettempdir()) / f'platen-native-slots-{os.getuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Unsafe native capacity directory')
    deadline = time.monotonic() + timeout
    descriptor = None
    try:
        while descriptor is None:
            check()
            for index in range(1):
                fd = os.open(root / str(index), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                else:
                    descriptor = fd
                    break
            if descriptor is None:
                if time.monotonic() >= deadline:
                    from app.core.ocr_v2.errors import OCRTimeoutError
                    raise OCRTimeoutError('Native processing capacity wait timed out')
                time.sleep(.05)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)


def execute(operation, *args, **kwargs):
    from app.core.native_operations import OPERATIONS
    from app.core.ocr_v2.geometry import _raster_context
    from app.core.ocr_v2.errors import EngineUnavailableError, OCRTimeoutError
    if operation not in OPERATIONS:
        raise ValueError('Unsupported isolated native operation')
    options = dict(kwargs)
    callbacks = {}
    for name in ('cancellation_check', 'page_progress_callback', 'progress_callback', 'progress_cb', 'after_ocr'):
        callback = options.pop(name, None)
        if callback is not None:
            if not callable(callback):
                raise ValueError('Invalid native callback')
            callbacks[name] = callback
    limit, context_cancel = _raster_context.get()
    def check():
        for callback in (context_cancel, callbacks.get('cancellation_check')):
            if callback is not None:
                callback()
    check()
    from app.core.native_domain import admit, staged_directory, DomainBusy, DomainUnavailable
    pending = None
    try:
        with admit(check, 30, lambda: _capacity(check, 30)) as domain:
            with staged_directory(domain, prefix='platen-native-') as directory:
                root = Path(directory)
                scratch = root / 'tmp'
                scratch.mkdir()
                arguments = list(args)
                # Native output and renderer sibling temporaries stay in the owned
                # job directory. Publish only after successful exit and result decode.
                output = None
                output_name = OPERATIONS[operation][2]
                if output_name:
                    if len(arguments) >= 2:
                        output = Path(arguments[1])
                        arguments[1] = str(scratch / ('output' + output.suffix))
                    elif output_name in options:
                        output = Path(options[output_name])
                        options[output_name] = str(scratch / ('output' + output.suffix))
                request = {'operation': operation, 'args': encode(arguments), 'kwargs': encode(options),
                           'callbacks': list(callbacks), 'raster_limit': limit}
                write_frame(root / 'request.json', request)
                events = 0
                total_event_bytes = 0
                def poll():
                    nonlocal events, total_event_bytes
                    check()
                    event_path = root / 'event.json'
                    if event_path.exists():
                        events += 1
                        total_event_bytes += event_path.stat().st_size
                        if events > MAX_EVENTS or total_event_bytes > 64 * 1024 * 1024:
                            raise ValueError('Native callback budget exceeded')
                        event = read_frame(event_path)
                        from app.controller.events import validate_event
                        validate_event(event, events)
                        callback = callbacks[event['name']]
                        if event['name'] == 'cancellation_check':
                            raise ValueError('Invalid native callback')
                        callback(*decode(event['args']))
                        event_path.unlink()  # Acknowledges validation/progress before continuation.
                env = os.environ.copy()
                env.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch),
                           OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                           OCR_PAGE_WORKERS='1', GLOBAL_TESSERACT_CAPACITY='1')
                env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
                try:
                    completed = domain.run(
                        [sys.executable, '-m', 'app.core.native_supervisor', str(root), str(os.getpid())],
                        timeout=CHILD_WALL, cancellation_check=poll, env=env,
                        max_output_bytes=64 * 1024, cleanup_group_on_exit=True,
                        legacy_runner=run_hardened_subprocess,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise OCRTimeoutError('Isolated native processing timed out') from exc
                domain.drain()
                if completed.returncode != 0 or not (root / 'result.json').is_file():
                    raise EngineUnavailableError('Isolated native processing failed or exceeded resource limits')
                frame = read_frame(root / 'result.json')
                if 'error' in frame:
                    raise_error(frame['error'])
                result = decode(frame['result'])
                check()
                if output is not None:
                    staged = scratch / ('output' + output.suffix)
                    if not staged.exists() or not stat.S_ISREG(staged.lstat().st_mode) or staged.stat().st_size > CHILD_FILES:
                        raise EngineUnavailableError('Isolated native output is missing or oversized')
                    output.parent.mkdir(parents=True, exist_ok=True)
                    fd, sibling_name = tempfile.mkstemp(prefix='.native-output-', dir=output.parent)
                    sibling = Path(sibling_name)
                    try:
                        with os.fdopen(fd, 'wb') as destination, staged.open('rb') as source:
                            shutil.copyfileobj(source, destination, length=64 * 1024)
                            destination.flush()
                            os.fsync(destination.fileno())
                        if domain.required:
                            pending = sibling
                        else:
                            os.replace(sibling, output)
                    finally:
                        if sibling != pending:
                            sibling.unlink(missing_ok=True)
            if domain.required:
                check()
            domain.cleanup_complete()
        # Hard-mode publication follows domain drainage, validation, staging
        # cleanup and controller release. Legacy publication keeps its old order.
        if pending is not None:
            check()
            os.replace(pending, output)
        return result
    except DomainBusy as exc:
        raise OCRTimeoutError('Native processing capacity wait timed out') from exc
    except DomainUnavailable as exc:
        raise EngineUnavailableError('Native execution domain is unavailable') from exc
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


async def execute_async(operation, *args, **kwargs):
    """Wait off-loop; cancellation joins supervision before callers clean inputs."""
    cancelled = threading.Event()
    original = kwargs.pop('cancellation_check', None)
    def check():
        from app.jobs.cancellation import JobCancelledException
        if cancelled.is_set():
            raise JobCancelledException('Native request cancelled')
        if original is not None:
            original()
    required = os.environ.get('NATIVE_EXECUTION_DOMAIN_MODE', 'legacy') != 'legacy'
    task = asyncio.create_task(asyncio.to_thread(execute, operation, *args, cancellation_check=check, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        if not required:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise

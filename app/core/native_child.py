"""Fresh native child: apply mandatory OS limits before importing PDF/OCR code."""
from __future__ import annotations
import importlib
import inspect
from pathlib import Path
import resource
import sys
import time


def main():
    from app.core import native_isolation as boundary
    def tighten(kind, ceiling):
        _, inherited = resource.getrlimit(kind)
        limit = ceiling if inherited == resource.RLIM_INFINITY else min(ceiling, inherited)
        resource.setrlimit(kind, (limit, limit))
    tighten(resource.RLIMIT_AS, boundary.CHILD_MEMORY)
    tighten(resource.RLIMIT_CPU, boundary.CHILD_CPU)
    tighten(resource.RLIMIT_FSIZE, boundary.CHILD_FILES)
    tighten(resource.RLIMIT_CORE, 0)
    tighten(resource.RLIMIT_NOFILE, 128)
    boundary.IN_CHILD = True
    root = Path(sys.argv[1])
    try:
        from app.core.native_operations import OPERATIONS
        from app.core.ocr_v2.geometry import raster_policy
        request = boundary.read_frame(root / 'request.json')
        module, function, _ = OPERATIONS[request['operation']]
        target = getattr(importlib.import_module(module), function)
        options = boundary.decode(request['kwargs'])
        for name in request['callbacks']:
            if name == 'cancellation_check':
                if name not in inspect.signature(target).parameters:
                    continue
                options[name] = lambda: None  # Parent polls the authoritative cancellation source.
            else:
                def callback(*args, _name=name):
                    boundary.write_frame(root / 'event.json', {'name': _name, 'args': boundary.encode(args)})
                    while (root / 'event.json').exists():
                        time.sleep(.01)
                options[name] = callback
        with raster_policy(request['raster_limit']):
            result = target(*boundary.decode(request['args']), **options)
        boundary.write_frame(root / 'result.json', {'result': boundary.encode(result)})
    except BaseException as exc:
        # Resource exhaustion may prevent even the error frame: the parent
        # treats absent/truncated output or abnormal exit as failure.
        boundary.write_frame(root / 'result.json', {'error': boundary.error_frame(exc)})


if __name__ == '__main__':
    main()

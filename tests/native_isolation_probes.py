"""Test-only child targets. Never registered by production code."""
import mmap
import os
from pathlib import Path
import resource
import sys
import time


def probe(directory, mode, **kwargs):
    from app.core.native_isolation import CHILD_MEMORY, CHILD_CPU
    assert resource.getrlimit(resource.RLIMIT_AS)[0] == CHILD_MEMORY
    assert resource.getrlimit(resource.RLIMIT_CPU)[0] == CHILD_CPU
    root = Path(directory)
    (root / 'child.pid').write_text(str(os.getpid()))
    if mode == 'hang':
        from app.core.subprocess_runner import run_hardened_subprocess
        code = "import os,time;from pathlib import Path;Path(%r).write_text(str(os.getpid()));time.sleep(60)" % str(root / 'grandchild.pid')
        run_hardened_subprocess([sys.executable, '-c', code])
    if mode == 'memory':
        # Reject a harmless 2 MiB virtual mapping under a deliberately tightened
        # limit. Never allocate a huge raster or exhaust physical memory.
        current = int(Path('/proc/self/statm').read_text().split()[0]) * os.sysconf('SC_PAGE_SIZE')
        resource.setrlimit(resource.RLIMIT_AS, (current + 1024 * 1024, current + 1024 * 1024))
        mmap.mmap(-1, 2 * 1024 * 1024)
        raise AssertionError('Address-space limit did not reject mapping')
    if mode == 'cpu':
        while True:
            pass
    if mode == 'orphan_crash':
        import subprocess
        code = "import os,time;from pathlib import Path;Path(%r).write_text(str(os.getpid()));time.sleep(30)" % str(root / 'grandchild.pid')
        child = subprocess.Popen([sys.executable, '-c', code])
        while not (root / 'grandchild.pid').exists():
            time.sleep(.01)
        os._exit(17)
    if mode == 'crash':
        os._exit(17)
    if mode == 'flood':
        print('x' * (128 * 1024), flush=True)
    return {'pid': os.getpid(), 'memory': CHILD_MEMORY, 'cpu': CHILD_CPU}

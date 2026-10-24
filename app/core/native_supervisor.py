"""Small non-native guardian: owner death must not orphan a native process tree."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main(child_command=None):
    from app.core.native_isolation import CHILD_WALL
    owner = int(sys.argv[2])
    deadline = time.monotonic() + CHILD_WALL
    # The hardened runner creates our process group. Never signal the caller's
    # group if this module is accidentally invoked without that boundary.
    if os.getpgrp() != os.getpid():
        raise RuntimeError('Native supervisor requires a private process group')
    child = None
    try:
        if os.getppid() != owner:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        child = subprocess.Popen(child_command or [
            sys.executable, '-m', 'app.core.native_child', sys.argv[1],
        ])  # Native child and its helpers inherit our supervised group.
        while True:
            if os.getppid() != owner or time.monotonic() >= deadline:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            code = child.poll()  # Reap before publishing exit to the parent.
            if code is not None:
                return code
            time.sleep(.05)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


if __name__ == '__main__':
    sys.exit(main())

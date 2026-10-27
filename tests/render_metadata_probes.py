"""Disposable child targets used only by render-session supervision tests."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time


def main():
    from app.core.native_child import apply_resource_limits
    from app.core.native_isolation import CHILD_MEMORY, CHILD_CPU
    apply_resource_limits()
    assert resource.getrlimit(resource.RLIMIT_AS)[0] == CHILD_MEMORY
    assert resource.getrlimit(resource.RLIMIT_CPU)[0] == CHILD_CPU
    root, mode = Path(sys.argv[1]), sys.argv[2]
    (root / "child.pid").write_text(str(os.getpid()))
    if mode in {"hang", "crash_tree"}:
        code = (
            "from pathlib import Path;import os,time;"
            f"Path({str(root / 'grandchild.pid')!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        subprocess.Popen([sys.executable, "-c", code])
        deadline = time.monotonic() + 5
        while not (root / "grandchild.pid").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Probe descendant did not start")
            time.sleep(.01)
    (root / "ready").touch()
    if mode == "hang":
        time.sleep(60)
    if mode in {"crash", "crash_tree"}:
        os._exit(17)
    if mode == "flood":
        print("x" * 4096, flush=True)
        return
    print(json.dumps({"page_count": 2}), flush=True)


if __name__ == "__main__":
    main()

"""Fixed native-UID entry point for every source-registered operation.

All names and arguments were schema checked by the bridge; validate again before
calling the same dispatcher used by the legacy subprocess path. No native code
runs in the broker/monitor. This file is part of the operator runtime digest.
"""
from pathlib import Path
import json
import os
import sys
from app.controller.profiles import ROOT, validate_manifest


def main(input_root=Path('/native-input'), work_root=Path(ROOT)):
    request=(input_root / 'request.json').read_bytes()
    files={p.name:p.read_bytes() for p in input_root.iterdir() if p.name != 'request.json'}
    from app.controller.profiles import VERSION
    validate_manifest({'kind':'native_operation','profile':VERSION,'request':request,'files':files})
    work_root.mkdir(mode=0o700)
    for name,data in files.items():
        (work_root / name).write_bytes(data)
    from app.controller.profiles import plain, encoded, PROFILES
    document=json.loads(request)
    values=plain(document['kwargs'])
    p=PROFILES[document['operation']]
    for name in p.inputs + ((p.output,) if p.output else ()):
        paths=values[name] if name in p.sequences else [values[name]]
        replaced=[str(work_root / Path(path).name) for path in paths]
        values[name]=replaced if name in p.sequences else replaced[0]
    document['kwargs']=encoded(values)
    (work_root / 'request.json').write_text(json.dumps(document))
    os.chdir(work_root)
    sys.argv=['native_child',str(work_root)]
    from app.core.native_child import main as dispatch
    dispatch()

if __name__ == '__main__':
    main()

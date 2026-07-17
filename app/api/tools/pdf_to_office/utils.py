from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass


@dataclass(slots=True)
class TempPaths:
    input_path: str
    output_path: str



def create_temp_paths(output_suffix: str) -> TempPaths:
    input_fd, input_path = tempfile.mkstemp(suffix=".pdf")
    os.close(input_fd)

    output_fd, output_path = tempfile.mkstemp(suffix=output_suffix)
    os.close(output_fd)

    return TempPaths(input_path=input_path, output_path=output_path)



def cleanup_paths(*paths: str) -> None:
    for path in paths:
        with suppress(Exception):
            if path and os.path.exists(path):
                os.remove(path)

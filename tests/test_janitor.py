from __future__ import annotations

import os
import tempfile
import time
from uuid import uuid4
from app.api.tools.editor.utils import get_temp_dir
from app.core.janitor import sweep_worker_temp_files


def test_worker_janitor_sweeps_expired_files() -> None:
    target_dir = get_temp_dir()

    # Create expired temp file (mtime = 2 hours ago)
    expired_file = os.path.join(target_dir, "pdfnest-source-test-expired.pdf")
    with open(expired_file, "w") as f:
        f.write("expired content")

    old_time = time.time() - 7200
    os.utime(expired_file, (old_time, old_time))

    # Create active temp file (mtime = now)
    active_file = os.path.join(target_dir, "pdfnest-source-test-active.pdf")
    with open(active_file, "w") as f:
        f.write("active content")

    try:
        evicted = sweep_worker_temp_files(file_ttl_seconds=3600)
        assert evicted >= 1
        assert not os.path.exists(expired_file)
        assert os.path.exists(active_file)
    finally:
        if os.path.exists(active_file):
            os.remove(active_file)


def test_worker_janitor_symlink_safety() -> None:
    target_dir = get_temp_dir()

    # Create a target file outside
    external_file = os.path.join(tempfile.gettempdir(), "user-important-doc.txt")
    with open(external_file, "w") as f:
        f.write("user data")

    symlink_path = os.path.join(target_dir, "pdfnest-source-symlink.pdf")
    try:
        os.symlink(external_file, symlink_path)
        sweep_worker_temp_files(file_ttl_seconds=3600)

        # Symlink should be removed, but external target file preserved
        assert not os.path.exists(symlink_path)
        assert os.path.exists(external_file)
    finally:
        if os.path.exists(external_file):
            os.remove(external_file)


def test_worker_janitor_preserves_local_storage_root() -> None:
    storage_root = os.path.join(tempfile.gettempdir(), "pdfnest-storage")
    studio_dir = os.path.join(storage_root, "studio")
    sources_dir = os.path.join(studio_dir, "sources")
    root_preexisting = os.path.exists(storage_root)
    studio_preexisting = os.path.exists(studio_dir)
    sources_preexisting = os.path.exists(sources_dir)
    marker = os.path.join(sources_dir, f"janitor-regression-{uuid4().hex}.pdf")
    os.makedirs(sources_dir, exist_ok=True)
    with open(marker, "w") as f:
        f.write("local Studio object")

    old_time = time.time() - 7200
    os.utime(storage_root, (old_time, old_time))
    os.utime(marker, (old_time, old_time))

    try:
        evicted = sweep_worker_temp_files(file_ttl_seconds=3600)
        assert evicted == 0
        assert os.path.exists(marker)
    finally:
        if os.path.exists(marker):
            os.remove(marker)
        if not sources_preexisting and os.path.isdir(sources_dir) and not os.listdir(sources_dir):
            os.rmdir(sources_dir)
        if not studio_preexisting and os.path.isdir(studio_dir) and not os.listdir(studio_dir):
            os.rmdir(studio_dir)
        if not root_preexisting and os.path.isdir(storage_root) and not os.listdir(storage_root):
            os.rmdir(storage_root)

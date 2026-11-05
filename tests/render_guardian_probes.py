"""Inject synthetic native commands while retaining the real guardian/runner."""
import os
import subprocess
import sys


def patch_render_child(monkeypatch, command_factory):
    from app.core import subprocess_runner
    original_run = subprocess_runner.run_hardened_subprocess
    original_popen = subprocess.Popen
    processes = []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def run(command, **options):
        native = command_factory(command)
        return original_run([sys.executable, "-m", "app.api.tools.render.supervisor",
                             str(os.getpid()), *native], **options)

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess_runner, "run_hardened_subprocess", run)
    return processes

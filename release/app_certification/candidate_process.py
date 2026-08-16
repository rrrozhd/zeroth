"""Bound the low-privilege candidate importer process."""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile

_OUTPUT_LIMIT = 1 << 20


def _limit_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (_OUTPUT_LIMIT, _OUTPUT_LIMIT))


def run_importer(argv: list[str]) -> tuple[int, str, str]:
    """Run one importer with bounded output and a hard timeout."""
    with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(
        mode="w+"
    ) as stderr:
        process = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            text=True,
            env={**os.environ, "APP_CERTIFICATION_IMPORTER": "1"},
            preexec_fn=_limit_output,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=150)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        stdout.seek(0)
        stderr.seek(0)
        output, diagnostics = stdout.read(_OUTPUT_LIMIT + 1), stderr.read(_OUTPUT_LIMIT + 1)
    if timed_out:
        return 1, output, diagnostics or "candidate importer timed out"
    if len(output) > _OUTPUT_LIMIT or len(diagnostics) > _OUTPUT_LIMIT:
        return 1, "", "candidate output exceeded limit"
    return process.returncode, output, diagnostics

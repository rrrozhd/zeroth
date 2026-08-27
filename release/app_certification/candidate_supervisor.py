"""Certifier-owned process boundary for candidate migration effects."""

from __future__ import annotations

import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from ctypes import CDLL, get_errno
from pathlib import Path

_OUTPUT_LIMIT = 1 << 20
_CPU_LIMIT = 120
_MEMORY_LIMIT = 2 * 1024 * 1024 * 1024
_PROCESS_LIMIT = 128
_OPEN_FILE_LIMIT = 256
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PROBE_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "venv=pathlib.Path(sys.argv.pop(1));"
    "site_packages=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/"
    "'site-packages';"
    "sys.prefix=sys.exec_prefix=str(venv);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(site_packages)];"
    "runpy.run_module('release.app_certification.candidate_process',run_name='__main__')"
)
_ROOT_BOUNDARY_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "trusted_site=pathlib.Path(sys.argv.pop(1)).resolve(strict=True);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(trusted_site)];"
    "runpy.run_module('release.app_certification.candidate_supervisor',run_name='__main__')"
)


def _cap_resource(kind: int, limit: int) -> None:
    soft, hard = resource.getrlimit(kind)
    capped_hard = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
    capped_soft = capped_hard if soft == resource.RLIM_INFINITY else min(capped_hard, soft)
    resource.setrlimit(kind, (capped_soft, capped_hard))


def _limit_resources() -> None:
    limits = [
        (resource.RLIMIT_FSIZE, _OUTPUT_LIMIT),
        (resource.RLIMIT_CPU, _CPU_LIMIT),
        (resource.RLIMIT_NOFILE, _OPEN_FILE_LIMIT),
    ]
    if sys.platform != "darwin":
        limits.extend(
            (
                (resource.RLIMIT_AS, _MEMORY_LIMIT),
                (resource.RLIMIT_NPROC, _PROCESS_LIMIT),
            )
        )
    for kind, limit in limits:
        _cap_resource(kind, limit)


def _terminate_process(
    process: subprocess.Popen[bytes], *, terminate_exited_group: bool = False
) -> None:
    if process.poll() is None or terminate_exited_group:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def _terminate_candidate_user(user: str) -> None:
    """Use the candidate UID only as a fail-closed post-run leak signal."""
    if _USER.fullmatch(user) is None:
        raise ValueError("untrusted user must be a simple local account name")
    remaining = subprocess.run(
        ["pgrep", "-u", user],
        check=False,
        capture_output=True,
        text=True,
    )
    if remaining.returncode == 1:
        return
    if remaining.returncode != 0:
        raise RuntimeError("candidate-user process inventory failed")
    process_ids = (remaining.stdout or "").split()
    if not process_ids or any(not process_id.isdecimal() for process_id in process_ids):
        raise RuntimeError("candidate-user process inventory was malformed")
    raise RuntimeError("candidate-user process leak survived the run-owned boundary")


def _enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("candidate process containment requires Linux pidfd support")
    library = CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(get_errno(), "cannot establish candidate process subreaper")


def _direct_child_pids() -> tuple[int, ...]:
    children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text(encoding="ascii")
    return tuple(int(pid) for pid in children.split())


def _terminate_run_children() -> None:
    deadline = time.monotonic() + 5
    while children := _direct_child_pids():
        for pid in children:
            try:
                pidfd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                with suppress(ChildProcessError, ProcessLookupError):
                    os.waitpid(pid, os.WNOHANG)
            finally:
                os.close(pidfd)
        if time.monotonic() >= deadline:
            raise RuntimeError("run-owned candidate processes survived cleanup")
        time.sleep(0.01)


def _wait_process(
    argv: list[str],
    *,
    stdout: int,
    candidate_user: str | None = None,
    timeout_seconds: int = 150,
) -> tuple[int, str, bool]:
    if candidate_user is not None:
        _enable_subreaper()
    with tempfile.TemporaryFile(mode="w+b") as stderr:
        process = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            text=False,
            preexec_fn=_limit_resources,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _terminate_process(process, terminate_exited_group=candidate_user is None)
            if candidate_user is not None:
                _terminate_run_children()
                _terminate_candidate_user(candidate_user)
        stderr.seek(0)
        diagnostics = stderr.read(_OUTPUT_LIMIT + 1).decode(errors="replace")
    if len(diagnostics) > _OUTPUT_LIMIT:
        return 1, "candidate diagnostics exceeded limit", timed_out
    return process.returncode, diagnostics, timed_out


def run_importer(
    argv: list[str], *, candidate_user: str | None = None, timeout_seconds: int = 150
) -> tuple[int, str, str]:
    """Run a bounded subprocess and retain only bounded diagnostic output."""
    with tempfile.TemporaryFile() as stdout:
        returncode, diagnostics, timed_out = _wait_process(
            argv,
            stdout=stdout.fileno(),
            candidate_user=candidate_user,
            timeout_seconds=timeout_seconds,
        )
        stdout.seek(0)
        raw = stdout.read(_OUTPUT_LIMIT + 1)
    if timed_out:
        return 1, raw.decode(errors="replace"), diagnostics or "candidate migration timed out"
    if len(raw) > _OUTPUT_LIMIT:
        return 1, "", "candidate output exceeded limit"
    return returncode, raw.decode(errors="replace"), diagnostics


def _probe_prefix(user: str | None) -> list[str]:
    if user is None:
        return []
    if _USER.fullmatch(user) is None:
        raise ValueError("untrusted user must be a simple local account name")
    environment = [
        f"{name}={os.environ[name]}"
        for name in ("ZEROTH_DATABASE__BACKEND", "ZEROTH_DATABASE__POSTGRES_DSN")
        if name in os.environ
    ]
    return [
        "sudo",
        "--non-interactive",
        "--user",
        user,
        "--",
        "/usr/bin/setpriv",
        "--no-new-privs",
        "--bounding-set=-all",
        "--ambient-caps=-all",
        "--inh-caps=-all",
        "env",
        "-i",
        f"HOME=/home/{user}",
        "LANG=C.UTF-8",
        f"PATH={os.environ.get('PATH', '')}",
        *environment,
    ]


def _root_boundary_argv(user: str, inner: list[str]) -> list[str]:
    return [
        "sudo",
        "--non-interactive",
        "--",
        str(Path(sys.executable).absolute()),
        "-I",
        "-S",
        "-c",
        _ROOT_BOUNDARY_BOOTSTRAP,
        str(Path(__file__).parents[2].resolve()),
        str(
            Path(sys.executable).absolute().parent.parent
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        ),
        "--root-boundary",
        user,
        "--",
        *inner,
    ]


def probe_candidate(
    name: str,
    root: Path,
    candidate_venv: Path,
    *,
    reference: str,
    database_url: str,
    untrusted_user: str | None = None,
) -> None:
    """Run a migration whose only authoritative result is the inspected database."""
    inner = [
        str(Path(sys.executable).absolute()),
        "-I",
        "-S",
        "-c",
        _PROBE_BOOTSTRAP,
        str(Path(__file__).parents[2].resolve()),
        str(candidate_venv.resolve()),
        name,
        "--root",
        str(root),
        "--reference",
        reference,
        "--database-url",
        database_url,
    ]
    argv = inner if untrusted_user is None else _root_boundary_argv(untrusted_user, inner)
    returncode, raw, diagnostics = run_importer(argv, timeout_seconds=180)
    if returncode:
        raise ValueError(diagnostics.strip() or raw.strip() or "candidate migration failed")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 4 or arguments[0] != "--root-boundary" or arguments[2] != "--":
        raise ValueError("invalid root-owned candidate boundary arguments")
    if os.geteuid() != 0:
        raise PermissionError("candidate process boundary must run as root")
    returncode, raw, diagnostics = run_importer(
        [*_probe_prefix(arguments[1]), *arguments[3:]],
        candidate_user=arguments[1],
    )
    sys.stdout.write(raw)
    sys.stderr.write(diagnostics)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""Run candidate dependency hooks inside one bounded Docker/cgroup scope."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

_OUTPUT_LIMIT = 1 << 20


def _cleanup_container(docker: str, container_name: str) -> None:
    with suppress(subprocess.TimeoutExpired):
        subprocess.run(
            [docker, "rm", "-f", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )


def _drain_output(stream: BinaryIO, retained: bytearray, limit: int) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            available = limit - len(retained)
            if available > 0:
                retained.extend(chunk[:available])
    except (OSError, ValueError):
        pass


def run_bounded_container(
    argv: list[str],
    *,
    docker: str,
    container_name: str,
    log_path: Path,
    timeout: float,
    output_limit: int = _OUTPUT_LIMIT,
) -> None:
    """Drain bounded diagnostics and always destroy the complete container cgroup."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    retained = bytearray()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    assert process.stdout is not None
    reader = threading.Thread(
        target=_drain_output, args=(process.stdout, retained, output_limit), daemon=True
    )
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    finally:
        _cleanup_container(docker, container_name)
        reader.join(timeout=5)
        if reader.is_alive() and process.stdout is not None:
            process.stdout.close()
            reader.join(timeout=1)
        log_path.write_bytes(retained)
    if reader.is_alive():
        raise RuntimeError("candidate dependency sandbox output reader did not terminate")
    if timed_out:
        raise RuntimeError("candidate dependency sandbox timed out")
    if process.returncode:
        raise RuntimeError(f"candidate dependency sandbox exited {process.returncode}")


def _run_checked(argv: list[str]) -> None:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"dependency sandbox setup failed: {detail}")


def _container_argv(args: argparse.Namespace, venv: Path) -> list[str]:
    return [
        args.docker,
        "run",
        "--name",
        args.container_name,
        "--init",
        "--cpus",
        str(args.cpus),
        "--memory",
        args.memory,
        "--memory-swap",
        args.memory,
        "--pids-limit",
        str(args.pids_limit),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{args.uid}:{args.gid}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--mount",
        f"type=bind,src={args.app_root.resolve()},dst=/app,readonly",
        "--mount",
        f"type=bind,src={venv.resolve()},dst=/work",
        "--mount",
        f"type=bind,src={args.uv.resolve()},dst=/usr/local/bin/uv,readonly",
        "--env",
        "HOME=/work/.home",
        "--env",
        "UV_CACHE_DIR=/work/.uv-cache",
        "--env",
        "UV_PROJECT_ENVIRONMENT=/work",
        args.image,
        "/usr/local/bin/uv",
        "sync",
        "--directory",
        "/app",
        "--frozen",
        "--all-extras",
    ]


def run_dependency_sandbox(args: argparse.Namespace) -> None:
    """Create a fixed-size filesystem and run dependency hooks in a container."""
    venv = args.app_root.resolve() / ".venv"
    if venv.exists() or venv.is_symlink():
        raise ValueError("candidate dependency environment already exists")
    args.disk.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(["truncate", "-s", str(args.disk_bytes), str(args.disk)])
    _run_checked(["mkfs.ext4", "-q", "-F", str(args.disk)])
    venv.mkdir()
    try:
        _run_checked(["sudo", "mount", "-o", "loop,nodev,nosuid", str(args.disk), str(venv)])
        _run_checked(["sudo", "chown", f"{args.uid}:{args.gid}", str(venv)])
        run_bounded_container(
            _container_argv(args, venv),
            docker=args.docker,
            container_name=args.container_name,
            log_path=args.log,
            timeout=args.timeout,
            output_limit=args.output_limit,
        )
    except Exception:
        cleanup_dependency_sandbox(args)
        raise


def cleanup_dependency_sandbox(args: argparse.Namespace) -> None:
    """Idempotently kill descendants and release only the named sandbox resources."""
    _cleanup_container(args.docker, args.container_name)
    venv = args.app_root.resolve() / ".venv"
    mounted = (
        subprocess.run(
            ["mountpoint", "-q", str(venv)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    if mounted:
        unmounted = subprocess.run(["sudo", "umount", str(venv)], check=False).returncode == 0
        still_mounted = (
            subprocess.run(
                ["mountpoint", "-q", str(venv)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not unmounted or still_mounted:
            raise RuntimeError("candidate dependency sandbox filesystem remains mounted")
    args.disk.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dependency-sandbox")
    parser.add_argument("command", choices=("run-dependency-sandbox", "cleanup-dependency-sandbox"))
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--uid", type=int, default=10001)
    parser.add_argument("--gid", type=int, default=10001)
    parser.add_argument("--image")
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--pids-limit", type=int, default=128)
    parser.add_argument("--disk-bytes", type=int, default=3 * 1024 * 1024 * 1024)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output-limit", type=int, default=_OUTPUT_LIMIT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run-dependency-sandbox":
            if args.image is None or args.uv is None or args.log is None:
                raise ValueError("run-dependency-sandbox requires image, uv, and log")
            run_dependency_sandbox(args)
        else:
            cleanup_dependency_sandbox(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"dependency sandbox error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run candidate dependency hooks inside one bounded Docker/cgroup scope."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

_OUTPUT_LIMIT = 1 << 20
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


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
    try:
        _run_bounded_process(argv, log_path=log_path, timeout=timeout, output_limit=output_limit)
    finally:
        _cleanup_container(docker, container_name)


def _run_bounded_process(
    argv: list[str], *, log_path: Path, timeout: float, output_limit: int
) -> None:
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


def _run_checked(argv: list[str], *, timeout: float = 60) -> str:
    completed = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=timeout
    )
    if len(completed.stdout) + len(completed.stderr) > _OUTPUT_LIMIT:
        raise RuntimeError("dependency sandbox setup output exceeded 1 MiB")
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"dependency sandbox setup failed: {detail}")
    return completed.stdout.strip()


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
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
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


def _builder_resources(args: argparse.Namespace) -> list[str]:
    quota = max(1, int(args.cpus * 100_000))
    return [
        "--driver-opt",
        f"memory={args.memory}",
        "--driver-opt",
        f"memory-swap={args.memory}",
        "--driver-opt",
        "cpu-period=100000",
        "--driver-opt",
        f"cpu-quota={quota}",
        "--driver-opt",
        "default-load=true",
        "--driver-opt",
        f"env.BUILDKIT_STEP_LOG_MAX_SIZE={args.output_limit}",
        "--driver-opt",
        f"env.BUILDKIT_STEP_LOG_MAX_SPEED={args.output_limit}",
    ]


def _builder_names(name: str) -> tuple[str, str]:
    if _NAME.fullmatch(name) is None:
        raise ValueError("builder name must contain only lowercase Docker name characters")
    return f"buildx_buildkit_{name}0", f"buildx_buildkit_{name}0_state"


def _mount_builder_disk(args: argparse.Namespace, mount: Path, volume: str) -> None:
    if args.disk.exists() or mount.exists():
        raise ValueError("candidate builder disk paths already exist")
    args.disk.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(["truncate", "-s", str(args.disk_bytes), str(args.disk)])
    _run_checked(["mkfs.ext4", "-q", "-F", str(args.disk)])
    mount.mkdir()
    _run_checked(["sudo", "mount", "-o", "loop,nodev,nosuid", str(args.disk), str(mount)])
    _run_checked(
        [
            args.docker,
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=none",
            "--opt",
            "o=bind",
            "--opt",
            f"device={mount.resolve()}",
            volume,
        ]
    )


def cleanup_build_sandbox(args: argparse.Namespace) -> None:
    """Remove only the named builder and its fixed-size state filesystem."""
    container, volume = _builder_names(args.builder_name)
    subprocess.run(
        [args.docker, "buildx", "rm", "--force", args.builder_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    _cleanup_container(args.docker, container)
    subprocess.run(
        [args.docker, "volume", "rm", "-f", volume],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    for argv in (
        [args.docker, "buildx", "inspect", args.builder_name],
        [args.docker, "inspect", container],
        [args.docker, "volume", "inspect", volume],
    ):
        if subprocess.run(
            argv,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).returncode == 0:
            raise RuntimeError("candidate builder resources survived cleanup")
    mount = args.disk.with_name(f"{args.disk.name}.mount")
    if mount.exists():
        mounted = subprocess.run(
            ["mountpoint", "-q", str(mount)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if mounted:
            _run_checked(["sudo", "umount", str(mount)])
        if subprocess.run(
            ["mountpoint", "-q", str(mount)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            raise RuntimeError("candidate builder filesystem remains mounted")
        mount.rmdir()
    args.disk.unlink(missing_ok=True)


def run_build_sandbox(args: argparse.Namespace) -> None:
    """Build one candidate image in a named, bounded, disposable BuildKit scope."""
    container, volume = _builder_names(args.builder_name)
    mount = args.disk.with_name(f"{args.disk.name}.mount")
    context = args.context.resolve()
    dockerfile = args.dockerfile.resolve()
    dockerfile.relative_to(context)
    if not context.is_dir() or not dockerfile.is_file():
        raise ValueError("candidate build context or Dockerfile is missing")
    try:
        _mount_builder_disk(args, mount, volume)
        _run_checked(
            [
                args.docker,
                "buildx",
                "create",
                "--name",
                args.builder_name,
                "--driver",
                "docker-container",
                *_builder_resources(args),
                "--bootstrap",
            ],
            timeout=120,
        )
        _run_checked(
            [args.docker, "update", "--pids-limit", str(args.pids_limit), container]
        )
        _run_bounded_process(
            [
                args.docker,
                "buildx",
                "build",
                "--builder",
                args.builder_name,
                "--file",
                str(dockerfile),
                "--tag",
                args.tag,
                "--load",
                str(context),
            ],
            log_path=args.log,
            timeout=args.timeout,
            output_limit=args.output_limit,
        )
    finally:
        cleanup_build_sandbox(args)


def _remove_resource(docker: str, argv: list[str]) -> None:
    subprocess.run(
        [docker, *argv],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _resource_exists(docker: str, kind: str, name: str) -> bool:
    command = {
        "container": ["container", "inspect"],
        "network": ["network", "inspect"],
        "volume": ["volume", "inspect"],
        "image": ["image", "inspect"],
    }[kind]
    return subprocess.run(
        [docker, *command, name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    ).returncode == 0


def _certification_resources(args: argparse.Namespace) -> list[tuple[str, str]]:
    return [
        ("container", "app-cert-wheel-inspect"),
        ("container", "app-cert-packaged"),
        ("container", "app-cert-ephemeral"),
        ("container", args.container_name),
        ("network", "app-cert-packaged-net"),
        ("network", "app-cert-ephemeral-net"),
        ("volume", "app-cert-packaged-data"),
        ("image", args.candidate_image),
        ("image", args.runtime_image),
    ]


def _remove_certification_resources(
    docker: str, resources: list[tuple[str, str]]
) -> None:
    for kind, name in resources:
        remove = {
            "container": ["rm", "-f"],
            "network": ["network", "rm"],
            "volume": ["volume", "rm", "-f"],
            "image": ["image", "rm", "-f"],
        }[kind]
        _remove_resource(docker, [*remove, name])


def _retained_resources(
    docker: str, resources: list[tuple[str, str]], errors: list[str]
) -> list[dict[str, str | bool]]:
    retained: list[dict[str, str | bool]] = []
    for kind, name in resources:
        absent = not _resource_exists(docker, kind, name)
        retained.append({"absent": absent, "kind": kind, "name": name})
        if not absent:
            errors.append(f"{kind} {name!r} survived cleanup")
    return retained


def cleanup_certification(args: argparse.Namespace) -> None:
    """Clean exact workflow resources and retain a machine-readable absence inventory."""
    errors: list[str] = []
    builder = argparse.Namespace(**vars(args))
    builder.disk = args.builder_disk
    try:
        cleanup_build_sandbox(builder)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(f"builder: {error}")
    try:
        cleanup_dependency_sandbox(args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(f"dependencies: {error}")
    resources = _certification_resources(args)
    _remove_certification_resources(args.docker, resources)
    retained = _retained_resources(args.docker, resources, errors)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "errors": errors,
                "resources": retained,
                "schema_version": 1,
                "status": "failed" if errors else "passed",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if errors:
        raise RuntimeError("; ".join(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dependency-sandbox")
    parser.add_argument(
        "command",
        choices=(
            "run-dependency-sandbox",
            "cleanup-dependency-sandbox",
            "run-build-sandbox",
            "cleanup-build-sandbox",
            "cleanup-certification",
        ),
    )
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
    parser.add_argument("--builder-name")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--dockerfile", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--builder-disk", type=Path)
    parser.add_argument("--candidate-image")
    parser.add_argument("--runtime-image")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run-dependency-sandbox":
            if args.image is None or args.uv is None or args.log is None:
                raise ValueError("run-dependency-sandbox requires image, uv, and log")
            run_dependency_sandbox(args)
        elif args.command == "cleanup-dependency-sandbox":
            cleanup_dependency_sandbox(args)
        elif args.command == "run-build-sandbox":
            if None in (args.builder_name, args.context, args.dockerfile, args.tag, args.log):
                raise ValueError(
                    "run-build-sandbox requires builder, context, Dockerfile, tag, and log"
                )
            run_build_sandbox(args)
        elif args.command == "cleanup-build-sandbox":
            if args.builder_name is None:
                raise ValueError("cleanup-build-sandbox requires builder name")
            cleanup_build_sandbox(args)
        else:
            if None in (
                args.builder_name,
                args.builder_disk,
                args.candidate_image,
                args.runtime_image,
                args.output,
            ):
                raise ValueError("cleanup-certification requires exact resource names and output")
            cleanup_certification(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"dependency sandbox error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

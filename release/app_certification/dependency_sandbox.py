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
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_NOT_FOUND = {
    "builder": re.compile(r"(?:no builder .+ found|no such builder)"),
    "container": re.compile(r"no such (?:object|container):"),
    "network": re.compile(r"network .+ not found"),
    "volume": re.compile(r"no such volume"),
    "image": re.compile(r"no such (?:object|image):"),
}


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
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
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


def _assert_builder_resources_absent(args: argparse.Namespace, container: str, volume: str) -> None:
    for kind, name in (
        ("builder", args.builder_name),
        ("container", container),
        ("volume", volume),
    ):
        if _resource_identity(args.docker, kind, name) is not None:
            raise RuntimeError("candidate builder resources survived cleanup")


def _cleanup_builder_mount(args: argparse.Namespace) -> None:
    mount = args.disk.with_name(f"{args.disk.name}.mount")
    if not mount.exists():
        args.disk.unlink(missing_ok=True)
        return
    mounted = (
        subprocess.run(
            ["mountpoint", "-q", str(mount)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    if mounted:
        _run_checked(["sudo", "umount", str(mount)])
    if (
        subprocess.run(
            ["mountpoint", "-q", str(mount)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        raise RuntimeError("candidate builder filesystem remains mounted")
    mount.rmdir()
    args.disk.unlink(missing_ok=True)


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
    _assert_builder_resources_absent(args, container, volume)
    _cleanup_builder_mount(args)


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
        _run_checked([args.docker, "update", "--pids-limit", str(args.pids_limit), container])
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


def _docker_daemon_id(docker: str) -> str:
    result = subprocess.run(
        [docker, "info", "--format", "{{.ID}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    daemon_id = result.stdout.strip()
    if result.returncode or not daemon_id:
        detail = result.stderr.strip() or result.stdout.strip() or "no daemon identity"
        raise RuntimeError(f"Docker daemon is unavailable: {detail[-500:]}")
    return daemon_id


def _resource_identity(docker: str, kind: str, name: str) -> str | None:
    command = {
        "builder": ["buildx", "inspect"],
        "container": ["container", "inspect", "--format", "{{.Id}}"],
        "network": ["network", "inspect", "--format", "{{.Id}}"],
        "volume": ["volume", "inspect", "--format", "{{.Name}}"],
        "image": ["image", "inspect", "--format", "{{.Id}}"],
    }[kind]
    result = subprocess.run(
        [docker, *command, name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        identity = result.stdout.strip()
        if kind == "builder" and not identity:
            identity = name
        if not identity:
            raise RuntimeError(f"Docker {kind} {name!r} returned no identity")
        return identity
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
    lowered = detail.lower()
    if _NOT_FOUND[kind].search(lowered):
        return None
    raise RuntimeError(f"Docker {kind} inspect failed for {name!r}: {detail[-500:]}")


def _remove_resource(
    docker: str, kind: str, name: str, *, expected_id: str | None = None
) -> str | None:
    observed = _resource_identity(docker, kind, name)
    if observed is None:
        return None
    if expected_id is not None and observed != expected_id:
        raise RuntimeError(
            f"created image identity mismatch for {name!r}: "
            f"expected {expected_id}, observed {observed}"
        )
    remove = {
        "builder": ["buildx", "rm", "--force"],
        "container": ["rm", "-f"],
        "network": ["network", "rm"],
        "volume": ["volume", "rm", "-f"],
        "image": ["image", "rm"],
    }[kind]
    result = subprocess.run(
        [docker, *remove, name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode and _resource_identity(docker, kind, name) is not None:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise RuntimeError(f"Docker {kind} removal failed for {name!r}: {detail[-500:]}")
    return observed


def _resource_exists(docker: str, kind: str, name: str) -> bool:
    return _resource_identity(docker, kind, name) is not None


def certification_resources(run_id: str) -> list[tuple[str, str]]:
    """Return the one canonical Docker inventory owned by a workflow run."""
    if _NAME.fullmatch(run_id) is None:
        raise ValueError("run ID must contain only lowercase Docker name characters")
    builder = f"app-cert-builder-{run_id}"
    builder_container, builder_volume = _builder_names(builder)
    return [
        ("builder", builder),
        ("container", builder_container),
        ("volume", builder_volume),
        ("container", f"app-cert-dependencies-{run_id}"),
        ("container", f"app-cert-wheel-inspect-{run_id}"),
        ("container", f"app-cert-packaged-{run_id}"),
        ("container", f"app-cert-ephemeral-{run_id}"),
        ("network", f"app-cert-packaged-net-{run_id}"),
        ("network", f"app-cert-ephemeral-net-{run_id}"),
        ("volume", f"app-cert-packaged-data-{run_id}"),
        ("image", f"app-cert-candidate:{run_id}"),
        ("image", f"app-cert-runtime:{run_id}"),
    ]


def _ownership_resources(run_id: str) -> list[dict[str, str]]:
    return [
        {"kind": kind, "name": name} for kind, name in certification_resources(run_id)
    ]


def _write_ownership(path: Path, *, daemon_id: str, run_id: str) -> None:
    if path.is_symlink() or path.exists():
        raise RuntimeError("certification ownership lease path already exists")
    document = {
        "daemon_id": daemon_id,
        "resources": _ownership_resources(run_id),
        "run_id": run_id,
        "schema_version": 1,
    }
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_ownership(path: Path, *, run_id: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("certification ownership lease is missing or unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("certification ownership lease is unreadable") from error
    expected_resources = _ownership_resources(run_id)
    if (
        not isinstance(document, dict)
        or set(document) != {"daemon_id", "resources", "run_id", "schema_version"}
        or document.get("schema_version") != 1
        or document.get("run_id") != run_id
        or document.get("resources") != expected_resources
        or not isinstance(document.get("daemon_id"), str)
        or not document["daemon_id"].strip()
    ):
        raise RuntimeError("certification ownership lease is invalid")
    return document["daemon_id"]


def check_certification_collisions(args: argparse.Namespace) -> None:
    """Fail before creation if any certifier-owned physical name is occupied."""
    daemon_id = _docker_daemon_id(args.docker)
    collisions = [
        f"{kind} {name!r} ({identity})"
        for kind, name in certification_resources(args.run_id)
        if (identity := _resource_identity(args.docker, kind, name)) is not None
    ]
    if collisions:
        raise RuntimeError("certification resource collision: " + ", ".join(collisions))
    _write_ownership(args.ownership, daemon_id=daemon_id, run_id=args.run_id)


def _image_ids(args: argparse.Namespace) -> dict[str, str | None]:
    values = {
        f"app-cert-candidate:{args.run_id}": args.candidate_image_id or None,
        f"app-cert-runtime:{args.run_id}": args.runtime_image_id or None,
    }
    for name, identity in values.items():
        if identity is not None and _DIGEST.fullmatch(identity) is None:
            raise ValueError(f"created image ID for {name!r} is not an immutable digest")
    return values


def _remove_certification_resources(
    docker: str,
    resources: list[tuple[str, str]],
    image_ids: dict[str, str | None],
    errors: list[str],
) -> dict[tuple[str, str], str | None]:
    created: dict[tuple[str, str], str | None] = {}
    for kind, name in resources:
        expected = image_ids.get(name)
        if kind == "image" and expected is None:
            errors.append(f"image {name!r} has no recorded created image identity")
            continue
        try:
            created[(kind, name)] = _remove_resource(docker, kind, name, expected_id=expected)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{kind} {name!r}: {error}")
    return created


def _retained_resources(
    docker: str,
    resources: list[tuple[str, str]],
    created: dict[tuple[str, str], str | None],
    image_ids: dict[str, str | None],
    errors: list[str],
) -> list[dict[str, str | bool | None]]:
    retained: list[dict[str, str | bool | None]] = []
    for kind, name in resources:
        try:
            absent = _resource_identity(docker, kind, name) is None
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            absent = False
            errors.append(f"{kind} {name!r} inventory: {error}")
        retained.append(
            {
                "absent": absent,
                "created_id": created.get((kind, name), image_ids.get(name)),
                "kind": kind,
                "name": name,
            }
        )
        if not absent:
            errors.append(f"{kind} {name!r} survived cleanup")
    return retained


def _cleanup_scopes(args: argparse.Namespace, errors: list[str]) -> None:
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


def _authorize_cleanup(args: argparse.Namespace, errors: list[str]) -> tuple[str | None, bool]:
    try:
        owned_daemon = _load_ownership(args.ownership, run_id=args.run_id)
    except (OSError, RuntimeError, ValueError) as error:
        errors.append(f"ownership lease: {error}")
        return None, False
    try:
        daemon_id = _docker_daemon_id(args.docker)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(str(error))
        return None, False
    if daemon_id != owned_daemon:
        errors.append(
            "ownership lease Docker daemon mismatch: "
            f"expected {owned_daemon!r}, observed {daemon_id!r}"
        )
        return daemon_id, False
    return daemon_id, True


def _unverified_inventory(
    resources: list[tuple[str, str]], image_ids: dict[str, str | None]
) -> list[dict[str, str | bool | None]]:
    return [
        {
            "absent": False,
            "created_id": image_ids.get(name),
            "kind": kind,
            "name": name,
        }
        for kind, name in resources
    ]


def cleanup_certification(args: argparse.Namespace) -> None:
    """Clean exact workflow resources and retain a machine-readable absence inventory."""
    errors: list[str] = []
    resources = certification_resources(args.run_id)
    image_ids = _image_ids(args)
    daemon_id, authorized = _authorize_cleanup(args, errors)
    if authorized:
        _cleanup_scopes(args, errors)
    created = (
        _remove_certification_resources(args.docker, resources, image_ids, errors)
        if authorized
        else {}
    )
    retained = (
        _retained_resources(args.docker, resources, created, image_ids, errors)
        if authorized
        else _unverified_inventory(resources, image_ids)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "daemon_id": daemon_id,
                "errors": errors,
                "resources": retained,
                "run_id": args.run_id,
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
            "check-certification-collisions",
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
    parser.add_argument("--candidate-image-id", default="")
    parser.add_argument("--runtime-image-id", default="")
    parser.add_argument("--run-id")
    parser.add_argument("--ownership", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _run_dependency_command(args: argparse.Namespace) -> None:
    if args.image is None or args.uv is None or args.log is None:
        raise ValueError("run-dependency-sandbox requires image, uv, and log")
    run_dependency_sandbox(args)


def _run_build_command(args: argparse.Namespace) -> None:
    if None in (args.builder_name, args.context, args.dockerfile, args.tag, args.log):
        raise ValueError("run-build-sandbox requires builder, context, Dockerfile, tag, and log")
    run_build_sandbox(args)


def _cleanup_build_command(args: argparse.Namespace) -> None:
    if args.builder_name is None:
        raise ValueError("cleanup-build-sandbox requires builder name")
    cleanup_build_sandbox(args)


def _collision_command(args: argparse.Namespace) -> None:
    if args.run_id is None or args.ownership is None:
        raise ValueError("check-certification-collisions requires run ID and ownership path")
    check_certification_collisions(args)


def _cleanup_certification_command(args: argparse.Namespace) -> None:
    if None in (args.builder_name, args.builder_disk, args.output, args.ownership, args.run_id):
        raise ValueError("cleanup-certification requires run ownership and output")
    cleanup_certification(args)


def _dispatch(args: argparse.Namespace) -> None:
    handlers = {
        "run-dependency-sandbox": _run_dependency_command,
        "cleanup-dependency-sandbox": cleanup_dependency_sandbox,
        "run-build-sandbox": _run_build_command,
        "cleanup-build-sandbox": _cleanup_build_command,
        "check-certification-collisions": _collision_command,
        "cleanup-certification": _cleanup_certification_command,
    }
    handlers[args.command](args)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _dispatch(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"dependency sandbox error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

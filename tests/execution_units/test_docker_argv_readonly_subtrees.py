"""ZER-37 Task 8: read-only subtree mounts on the docker backend.

``_run_in_docker`` bind-mounts the sandbox root read-write; a repository unit
needs designated subtrees remounted read-only on top of it. These tests pin
the argv the manager actually hands to ``docker create``: ro mounts appended
after the rw root with the ``:ro`` suffix, a BYTE-IDENTICAL argv when no
read-only paths are requested, lexical rejection of hostile subtree entries
before any docker invocation, and the LOCAL backend's unenforced-constraint
warning.
"""

from __future__ import annotations

import subprocess
import warnings
from io import BytesIO
from pathlib import Path, PurePosixPath

import pytest

from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxEnvironment,
    SandboxManager,
    SandboxPolicyViolationError,
    SandboxStrictnessMode,
)


class _FakeDockerProcess:
    def __init__(self) -> None:
        self.stdout = BytesIO(b"ok")
        self.stderr = BytesIO(b"")
        self.stdin = BytesIO()
        self.returncode: int | None = None

    def wait(self, timeout=None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def _docker_calls(
    sandbox_root: Path,
    *,
    read_only_paths: tuple[str, ...] | None = None,
) -> list[list[str]]:
    """Run one docker dispatch against a fake runner and return every argv."""
    calls: list[list[str]] = []

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[:4] == ["docker", "inspect", "-f", "{{.Config.Image}}"]:
            return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    manager = SandboxManager(
        base_env={},
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            strictness_mode=SandboxStrictnessMode.STANDARD,
        ),
        command_runner=fake_runner,
        process_factory=lambda command, **_kwargs: (
            calls.append(list(command)) or _FakeDockerProcess()
        ),
        container_inspector=lambda _name: True,
    )
    kwargs = {} if read_only_paths is None else {"read_only_paths": read_only_paths}
    manager._run_in_docker(
        command=["echo", "ok"],
        input_text=None,
        timeout_seconds=None,
        sandbox_root=sandbox_root,
        relative_cwd=None,
        environment=SandboxEnvironment(cache_key="cache", variables={}),
        **kwargs,
    )
    return calls


def _docker_create_argv(
    sandbox_root: Path, *, read_only_paths: tuple[str, ...] | None = None
) -> list[str]:
    calls = _docker_calls(sandbox_root, read_only_paths=read_only_paths)
    return next(command for command in calls if command[:2] == ["docker", "create"])


def _container_root(sandbox_root: Path) -> PurePosixPath:
    return PurePosixPath("/tmp/zeroth-sandbox") / sandbox_root.name


def test_read_only_subtrees_mount_after_the_rw_root_with_ro_suffix(tmp_path: Path) -> None:
    container_root = _container_root(tmp_path)

    argv = _docker_create_argv(tmp_path, read_only_paths=("vendor", "data/fixtures"))

    mounts = [argv[index + 1] for index, token in enumerate(argv) if token == "-v"]
    assert mounts == [
        f"{tmp_path}:{container_root}",
        f"{tmp_path}/vendor:{container_root}/vendor:ro",
        f"{tmp_path}/data/fixtures:{container_root}/data/fixtures:ro",
    ]
    # every mount is a docker flag, not a workload argument
    image_index = argv.index("python:3.12")
    for index, token in enumerate(argv):
        if token == "-v":
            assert index < image_index


def test_empty_default_leaves_the_argv_byte_identical(tmp_path: Path) -> None:
    """Empty read-only paths preserve all flags apart from the unique workload name."""
    container_root = _container_root(tmp_path)
    expected = [
        "docker",
        "create",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp",
        "-v",
        f"{tmp_path}:{container_root}",
        "--network",
        "none",
        "-w",
        str(container_root),
        "python:3.12",
        "echo",
        "ok",
    ]

    for argv in (_docker_create_argv(tmp_path), _docker_create_argv(tmp_path, read_only_paths=())):
        assert argv[2] == "--name"
        assert argv[3].startswith("zeroth-sandbox-run-")
        assert argv[:2] + argv[4:] == expected


@pytest.mark.parametrize(
    "hostile",
    [
        "../etc",
        "/absolute",
        "nested/../up",
        "a:b",
        "a\\b",
        "a\x00b",
        "",
        ".",
    ],
)
def test_hostile_read_only_subtrees_are_rejected_before_any_docker_call(
    tmp_path: Path, hostile: str
) -> None:
    with pytest.raises(SandboxPolicyViolationError, match="read-only"):
        _docker_calls(tmp_path, read_only_paths=(hostile,))


def test_hostile_subtree_rejection_precedes_every_docker_invocation(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")

    manager = SandboxManager(
        base_env={},
        config=SandboxConfig(backend=SandboxBackendMode.DOCKER),
        command_runner=fake_runner,
        process_factory=lambda command, **_kwargs: (
            calls.append(list(command)) or _FakeDockerProcess()
        ),
        container_inspector=lambda _name: True,
    )

    with pytest.raises(SandboxPolicyViolationError):
        manager._run_in_docker(
            command=["echo", "ok"],
            input_text=None,
            timeout_seconds=None,
            sandbox_root=tmp_path,
            relative_cwd=None,
            environment=SandboxEnvironment(cache_key="cache", variables={}),
            read_only_paths=("../etc",),
        )

    assert calls == []


def test_local_backend_warns_when_read_only_paths_are_requested(tmp_path: Path) -> None:
    manager = SandboxManager(
        base_env={},
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        ),
    )

    with pytest.warns(UserWarning, match="read-only path constraints"):
        manager._run_locally(
            command=["echo", "ok"],
            input_text=None,
            timeout_seconds=None,
            cwd=tmp_path,
            environment=SandboxEnvironment(cache_key="cache", variables={}),
            read_only_paths=("vendor",),
        )


def test_local_backend_stays_silent_without_read_only_paths(tmp_path: Path) -> None:
    manager = SandboxManager(
        base_env={},
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        ),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        manager._run_locally(
            command=["echo", "ok"],
            input_text=None,
            timeout_seconds=None,
            cwd=tmp_path,
            environment=SandboxEnvironment(cache_key="cache", variables={}),
        )

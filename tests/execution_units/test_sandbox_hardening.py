from __future__ import annotations

import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from zeroth.integrations.execution.constraints import (
    ResourceConstraints,
    build_docker_resource_flags,
)
from zeroth.integrations.execution.sandbox import (
    DockerSandboxConfig,
    SandboxBackendMode,
    SandboxBackendUnavailableError,
    SandboxConfig,
    SandboxManager,
    SandboxPolicyViolationError,
    SandboxStrictnessMode,
    SandboxTimeoutError,
)


def test_strict_mode_raises_when_docker_unavailable() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.AUTO,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
            strictness_mode=SandboxStrictnessMode.STRICT,
        ),
        container_inspector=lambda _name: False,
    )

    with pytest.raises(SandboxBackendUnavailableError):
        manager.run(["echo", "hello"])


def test_strict_mode_refuses_disabled_docker_hardening() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            docker=DockerSandboxConfig(hardened=False),
            strictness_mode=SandboxStrictnessMode.STRICT,
        ),
        container_inspector=lambda _name: True,
    )

    with pytest.raises(SandboxPolicyViolationError, match="hardening"):
        manager.run(["echo", "hello"])


def test_strict_sidecar_mode_refuses_missing_client() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.SIDECAR,
            strictness_mode=SandboxStrictnessMode.STRICT,
        )
    )

    with pytest.raises(SandboxBackendUnavailableError, match="sidecar client"):
        manager.run(["echo", "hello"])


def test_standard_mode_raises_when_docker_unavailable_without_local_fallback() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.AUTO,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
            strictness_mode=SandboxStrictnessMode.STANDARD,
        ),
        container_inspector=lambda _name: False,
    )

    with pytest.raises(SandboxBackendUnavailableError):
        manager.run(["echo", "hello"])


def test_permissive_mode_falls_back_to_local_when_docker_unavailable(tmp_path: Path) -> None:
    script = tmp_path / "echo.py"
    script.write_text("print('local-ok')", encoding="utf-8")
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.AUTO,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
            strictness_mode=SandboxStrictnessMode.PERMISSIVE,
        ),
        container_inspector=lambda _name: False,
    )

    result = manager.run([sys.executable, str(script)])

    assert result.backend == "local"
    assert result.stdout.strip() == "local-ok"


def test_build_docker_resource_flags_translates_supported_constraints() -> None:
    constraints = ResourceConstraints(
        cpu_cores=1.5,
        memory_mb=512,
        disk_mb=1024,
        max_processes=64,
        network_access=False,
    )

    assert build_docker_resource_flags(constraints) == [
        "--cpus",
        "1.5",
        "--memory",
        "512m",
        "--pids-limit",
        "64",
        "--network",
        "none",
    ]


def test_run_in_docker_applies_resource_constraint_flags() -> None:
    calls: list[list[str]] = []
    process = _FakeDockerProcess(stdout=b"docker-ok", stderr=b"")

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[:4] == ["docker", "inspect", "-f", "{{.Config.Image}}"]:
            return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 0, stdout="docker-ok", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.AUTO,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
            strictness_mode=SandboxStrictnessMode.STANDARD,
        ),
        command_runner=fake_runner,
        process_factory=lambda command, **_kwargs: calls.append(list(command)) or process,
        container_inspector=lambda _name: True,
    )

    result = manager.run(
        ["python", "-m", "demo"],
        working_directory="job",
        resource_constraints=ResourceConstraints(
            cpu_cores=2.0,
            memory_mb=256,
            max_processes=32,
            network_access=False,
        ),
    )

    assert result.backend == "docker"
    docker_run = next(command for command in calls if command[:2] == ["docker", "run"])
    assert "--cpus" in docker_run
    assert "--memory" in docker_run
    assert "--pids-limit" in docker_run
    assert "--network" in docker_run
    assert "python:3.12" in docker_run


@pytest.mark.parametrize(
    ("constraints", "expected_network"),
    [
        (None, "none"),
        (ResourceConstraints(network_access=None), "none"),
        (ResourceConstraints(network_access=False), "none"),
        (ResourceConstraints(network_access=True), "bridge"),
    ],
)
def test_strict_docker_denies_network_unless_explicitly_authorized(
    constraints: ResourceConstraints | None, expected_network: str
) -> None:
    calls: list[list[str]] = []
    process = _FakeDockerProcess(stdout=b"ok", stderr=b"")

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[:4] == ["docker", "inspect", "-f", "{{.Config.Image}}"]:
            return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            strictness_mode=SandboxStrictnessMode.STRICT,
        ),
        command_runner=fake_runner,
        process_factory=lambda command, **_kwargs: calls.append(list(command)) or process,
        container_inspector=lambda _name: True,
    )

    manager.run(["echo", "ok"], resource_constraints=constraints)

    docker_run = next(command for command in calls if command[:2] == ["docker", "run"])
    index = docker_run.index("--network")
    assert docker_run[index + 1] == expected_network
    assert docker_run.count("--network") == 1


class _FakeDockerProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes, times_out: bool = False) -> None:
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)
        self.stdin = BytesIO()
        self.returncode: int | None = None
        self.times_out = times_out
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout=None) -> int:
        self.wait_calls += 1
        if self.times_out and not self.killed:
            raise subprocess.TimeoutExpired(["docker", "run"], timeout)
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.parametrize(
    ("stdout", "stderr", "cap", "expected_stdout", "expected_stderr", "flags"),
    [
        (b"stdout-overflow", b"ok", 6, "stdout", "ok", (True, False)),
        (b"ok", b"stderr-overflow", 6, "ok", "stderr", (False, True)),
        ("ééé".encode(), b"", 5, "éé", "", (True, False)),
    ],
)
def test_hardened_docker_bounds_output_without_splitting_utf8(
    stdout: bytes,
    stderr: bytes,
    cap: int,
    expected_stdout: str,
    expected_stderr: str,
    flags: tuple[bool, bool],
) -> None:
    process = _FakeDockerProcess(stdout=stdout, stderr=stderr)

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")

    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            docker=DockerSandboxConfig(max_output_bytes=cap),
            strictness_mode=SandboxStrictnessMode.STRICT,
        ),
        command_runner=fake_runner,
        process_factory=lambda *_args, **_kwargs: process,
        container_inspector=lambda _name: True,
    )

    result = manager.run(["echo", "output"])

    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert (result.stdout_truncated, result.stderr_truncated) == flags
    assert len(result.stdout.encode()) <= cap
    assert len(result.stderr.encode()) <= cap


def test_hardened_docker_timeout_kills_and_waits_for_child() -> None:
    process = _FakeDockerProcess(stdout=b"partial", stderr=b"", times_out=True)

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")

    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            strictness_mode=SandboxStrictnessMode.STRICT,
        ),
        command_runner=fake_runner,
        process_factory=lambda *_args, **_kwargs: process,
        container_inspector=lambda _name: True,
    )

    with pytest.raises(SandboxTimeoutError):
        manager.run(["sleep"], timeout_seconds=0.01)

    assert process.killed is True
    assert process.wait_calls == 2


def test_policy_violation_is_raised_when_required_isolation_cannot_be_met() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.LOCAL,
            strictness_mode=SandboxStrictnessMode.STRICT,
        )
    )

    with pytest.raises(SandboxPolicyViolationError):
        manager.run(
            ["echo", "hello"],
            resource_constraints=ResourceConstraints(network_access=False),
        )


def test_strict_refuses_local_backend_even_without_constraints() -> None:
    # S2: STRICT + LOCAL must refuse UNCONDITIONALLY. A bare inline unit reaches
    # the sandbox with resource_constraints=None; gating the refusal on
    # constraints made strict mode a silent no-op that ran authored code as an
    # unisolated host subprocess.
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.LOCAL,
            strictness_mode=SandboxStrictnessMode.STRICT,
        )
    )

    with pytest.raises(SandboxPolicyViolationError, match="local backend"):
        manager.run(["echo", "UNISOLATED"])  # no resource_constraints


def test_standard_local_without_constraints_still_runs() -> None:
    # Guard the boundary: STANDARD (not STRICT) + LOCAL with no hard-isolation
    # constraints is still permitted, so the S2 fix doesn't over-restrict.
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.LOCAL,
            strictness_mode=SandboxStrictnessMode.STANDARD,
        )
    )
    result = manager.run(["echo", "ok"])
    assert result.backend == "local"


def test_docker_hardening_flags_applied_by_default() -> None:
    from zeroth.integrations.execution.sandbox import DockerSandboxConfig, _docker_hardening_flags

    flags = _docker_hardening_flags(DockerSandboxConfig())
    assert "--read-only" in flags
    assert flags[flags.index("--cap-drop") + 1] == "ALL"
    assert flags[flags.index("--security-opt") + 1] == "no-new-privileges"
    assert flags[flags.index("--tmpfs") + 1] == "/tmp"
    assert "--user" not in flags


def test_docker_hardening_flags_disabled_and_user_override() -> None:
    from zeroth.integrations.execution.sandbox import DockerSandboxConfig, _docker_hardening_flags

    assert _docker_hardening_flags(DockerSandboxConfig(hardened=False)) == []
    flags = _docker_hardening_flags(DockerSandboxConfig(hardened=False, run_as_user="65534:65534"))
    assert flags == ["--user", "65534:65534"]

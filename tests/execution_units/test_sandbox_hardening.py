from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from zeroth.core.execution_units.constraints import ResourceConstraints, build_docker_resource_flags
from zeroth.core.execution_units.sandbox import (
    DockerSandboxConfig,
    SandboxBackendMode,
    SandboxBackendUnavailableError,
    SandboxConfig,
    SandboxManager,
    SandboxPolicyViolationError,
    SandboxStrictnessMode,
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

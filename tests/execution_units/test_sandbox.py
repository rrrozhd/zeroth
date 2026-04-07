from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zeroth.execution_units.sandbox import (
    DockerSandboxConfig,
    EnvironmentCacheManager,
    SandboxBackendMode,
    SandboxBackendUnavailableError,
    SandboxConfig,
    SandboxManager,
    SandboxTimeoutError,
    compute_environment_cache_key,
)


def test_sandbox_manager_runs_in_temp_workdir_and_filters_environment(tmp_path: Path) -> None:
    script = tmp_path / "inspect.py"
    script.write_text(
        """
import json
import os
from pathlib import Path

cwd = Path.cwd()
(cwd / "created.txt").write_text("created")
print(json.dumps({
    "pid": os.getpid(),
    "cwd": str(cwd),
    "cwd_exists": cwd.exists(),
    "allowed": os.environ.get("ALLOWED"),
    "overlay": os.environ.get("OVERLAY"),
    "secret": os.environ.get("SECRET"),
}))
""".strip()
    )

    manager = SandboxManager(base_env={"ALLOWED": "yes", "SECRET": "no"})
    result = manager.run(
        [sys.executable, str(script)],
        allowed_env_keys=["ALLOWED"],
        overlay_env={"OVERLAY": "present"},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["pid"] != os.getpid()
    assert payload["cwd_exists"] is True
    assert payload["allowed"] == "yes"
    assert payload["overlay"] == "present"
    assert payload["secret"] is None
    assert result.environment == {"ALLOWED": "yes", "OVERLAY": "present"}
    assert result.backend == "local"
    assert not Path(result.workdir).exists()


def test_sandbox_manager_times_out_and_raises(tmp_path: Path) -> None:
    script = tmp_path / "sleep.py"
    script.write_text(
        """
import time

time.sleep(1)
""".strip()
    )

    manager = SandboxManager(base_env={"ALLOWED": "yes"})

    with pytest.raises(SandboxTimeoutError) as exc_info:
        manager.run(
            [sys.executable, str(script)],
            timeout_seconds=0.05,
            allowed_env_keys=["ALLOWED"],
        )

    assert exc_info.value.command[0] == sys.executable
    assert exc_info.value.timeout_seconds == 0.05


def test_environment_cache_manager_hits_misses_and_cache_keys_are_stable() -> None:
    manager = EnvironmentCacheManager()
    builder_calls = 0
    key = compute_environment_cache_key(
        runtime="python",
        runtime_version="3.12",
        dependency_manifest={"packages": ["pydantic"]},
        build_config={"command": ["python", "-m", "build"]},
        sandbox_policy={"network_access": False},
        identity={"allowlist": ["ALLOWED"]},
    )

    def builder() -> dict[str, str]:
        nonlocal builder_calls
        builder_calls += 1
        return {"ALLOWED": "yes"}

    first = manager.resolve(key, builder)
    second = manager.resolve(key, builder)

    assert builder_calls == 1
    assert first is second
    assert manager.get(key) is first
    assert first.variables == {"ALLOWED": "yes"}

    same_key = compute_environment_cache_key(
        runtime="python",
        runtime_version="3.12",
        dependency_manifest={"packages": ["pydantic"]},
        build_config={"command": ["python", "-m", "build"]},
        sandbox_policy={"network_access": False},
        identity={"allowlist": ["ALLOWED"]},
    )
    different_key = compute_environment_cache_key(
        runtime="python",
        runtime_version="3.12",
        dependency_manifest={"packages": ["pydantic", "pytest"]},
        build_config={"command": ["python", "-m", "build"]},
        sandbox_policy={"network_access": False},
        identity={"allowlist": ["ALLOWED"]},
    )

    assert same_key == key
    assert different_key != key


def test_sandbox_manager_auto_selects_provisioned_docker_backend() -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[:4] == ["docker", "inspect", "-f", "{{.Config.Image}}"]:
            return subprocess.CompletedProcess(command, 0, stdout="python:3.12\n", stderr="")
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 0, stdout="docker-ok", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    manager = SandboxManager(
        base_env={"ALLOWED": "yes"},
        config=SandboxConfig(
            backend=SandboxBackendMode.AUTO,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
        ),
        command_runner=fake_runner,
        container_inspector=lambda name: name == "zeroth-sandbox",
    )

    result = manager.run(
        ["python", "-m", "demo"],
        allowed_env_keys=["ALLOWED"],
        overlay_env={"OVERLAY": "present"},
        working_directory="job",
    )

    assert result.backend == "docker"
    assert result.container_name == "zeroth-sandbox"
    run_calls = [command for command in calls if command[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    run_command = run_calls[0]
    assert "-w" in run_command
    assert "python:3.12" in run_command
    assert "python" in run_command
    assert "-e" in run_command


def test_sandbox_manager_raises_when_docker_backend_is_requested_but_missing() -> None:
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            docker=DockerSandboxConfig(container_name="zeroth-sandbox"),
        ),
        container_inspector=lambda _name: False,
    )

    with pytest.raises(SandboxBackendUnavailableError, match="not available"):
        manager.run(["echo", "hello"])


# --- SIDECAR mode tests ---


def test_sidecar_enum_exists() -> None:
    """SandboxBackendMode.SIDECAR is a valid enum value."""
    assert SandboxBackendMode.SIDECAR == "sidecar"
    assert SandboxBackendMode.SIDECAR.value == "sidecar"


def test_resolve_backend_returns_sidecar_when_configured() -> None:
    """_resolve_backend returns SIDECAR when backend is configured and client provided."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    config = SandboxConfig(backend=SandboxBackendMode.SIDECAR)
    manager = SandboxManager(config=config, sidecar_client=mock_client)

    result = manager._resolve_backend()
    assert result is SandboxBackendMode.SIDECAR


def test_resolve_backend_raises_when_sidecar_no_client() -> None:
    """_resolve_backend raises when SIDECAR configured but no client provided."""
    config = SandboxConfig(backend=SandboxBackendMode.SIDECAR)
    manager = SandboxManager(config=config)

    with pytest.raises(SandboxBackendUnavailableError, match="sidecar client not configured"):
        manager._resolve_backend()


def test_run_via_sidecar_constructs_request_and_translates_response() -> None:
    """_run_via_sidecar builds correct SidecarExecuteRequest and maps response."""
    from unittest.mock import AsyncMock

    from zeroth.execution_units.constraints import ResourceConstraints
    from zeroth.sandbox_sidecar.models import SidecarExecuteResponse

    mock_response = SidecarExecuteResponse(
        execution_id="test-id",
        status="completed",
        returncode=0,
        stdout="output\n",
        stderr="",
        duration_seconds=1.0,
        timed_out=False,
    )
    mock_client = AsyncMock()
    mock_client.execute.return_value = mock_response

    config = SandboxConfig(backend=SandboxBackendMode.SIDECAR)
    manager = SandboxManager(
        config=config,
        sidecar_client=mock_client,
        base_env={"PATH": "/usr/bin"},
    )

    constraints = ResourceConstraints(
        cpu_cores=2.0,
        memory_mb=512,
        max_processes=100,
        network_access=False,
    )

    result = manager.run(
        ["python", "-c", "print('hello')"],
        resource_constraints=constraints,
    )

    assert result.backend == "sidecar"
    assert result.returncode == 0
    assert result.stdout == "output\n"
    assert result.timed_out is False
    assert result.duration_seconds == 1.0

    # Verify the request was constructed correctly
    call_args = mock_client.execute.call_args[0][0]
    assert call_args.command == ["python", "-c", "print('hello')"]
    assert call_args.cpu_cores == 2.0
    assert call_args.memory_mb == 512
    assert call_args.network_access is False


def test_sandbox_config_has_sidecar_url() -> None:
    """SandboxConfig supports sidecar_url field."""
    config = SandboxConfig(
        backend=SandboxBackendMode.SIDECAR,
        sidecar_url="http://localhost:8001",
    )
    assert config.sidecar_url == "http://localhost:8001"

"""settings.sandbox is actually consumed by the bootstrapped runner (audit P1).

Before the fix the factory built ``ExecutableUnitRunner()`` bare, so the sandbox
backend configured in settings was read by nobody and untrusted code always ran
on the default LOCAL backend. These tests pin that ``_build_sandbox_manager``
faithfully reflects the configured backend (and stays byte-equal to the bare
default under default settings) AND that the real factory hands the configured
manager to the runner end-to-end.
"""

from __future__ import annotations

import pytest

from zeroth.integrations.execution.sandbox import SandboxBackendMode, SandboxConfig
from zeroth.platform.config.settings import SandboxSettings, get_settings
from zeroth.service.bootstrap.factory import _build_sandbox_manager


def test_default_settings_produce_bare_sandbox_config() -> None:
    # The de-risking assertion for the whole bootstrap regression surface: default
    # settings yield a config byte-equal to the historical bare default.
    manager = _build_sandbox_manager(SandboxSettings())
    assert manager._config == SandboxConfig()
    assert manager._config.backend is SandboxBackendMode.LOCAL
    assert manager._sidecar_client is None


def test_docker_settings_configure_docker_backend() -> None:
    manager = _build_sandbox_manager(
        SandboxSettings(backend="docker", docker_container_name="foo", docker_binary="podman")
    )
    assert manager._config.backend is SandboxBackendMode.DOCKER
    assert manager._config.docker.container_name == "foo"
    assert manager._config.docker.docker_binary == "podman"
    assert manager._sidecar_client is None  # sidecar client is not built for docker


def test_sidecar_settings_construct_client_when_secret_present(monkeypatch) -> None:
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", "s3cr3t")
    manager = _build_sandbox_manager(
        SandboxSettings(backend="sidecar", sidecar_url="http://sandbox-sidecar:9000")
    )
    assert manager._config.backend is SandboxBackendMode.SIDECAR
    assert manager._config.sidecar_url == "http://sandbox-sidecar:9000"
    assert manager._sidecar_client is not None


def test_sidecar_without_secret_fails_closed(monkeypatch) -> None:
    # A sidecar deployment with no secret is a fail-closed boot error, not a silent
    # downgrade to LOCAL.
    monkeypatch.delenv("ZEROTH_SANDBOX_SIDECAR_SECRET", raising=False)
    with pytest.raises(ValueError):
        _build_sandbox_manager(SandboxSettings(backend="sidecar"))


@pytest.mark.asyncio
async def test_factory_wires_configured_backend_into_runner(sqlite_db, monkeypatch) -> None:
    from tests.service.helpers import agent_graph, deploy_service

    monkeypatch.setattr(
        get_settings(),
        "sandbox",
        SandboxSettings(backend="docker", docker_container_name="wired-check"),
    )
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="sandbox-wiring"))
    runner = service.orchestrator.executable_unit_runner
    assert runner.sandbox_manager._config.backend is SandboxBackendMode.DOCKER
    assert runner.sandbox_manager._config.docker.container_name == "wired-check"
    # (The injected-runner seam is preserved by the `executable_unit_runner or ...`
    # precedence and covered by tests/runtime/orchestration/test_characterization.py.)

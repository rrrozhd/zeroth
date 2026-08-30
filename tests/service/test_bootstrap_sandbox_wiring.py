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
from types import SimpleNamespace

from zeroth.integrations.execution.sandbox import SandboxBackendMode, SandboxConfig
from zeroth.platform.config.settings import (
    AuthSettings,
    SandboxSettings,
    ZerothSettings,
    get_settings,
)
from zeroth.service.api.authentication import ServiceAuthenticator
from zeroth.service.app import create_app
from zeroth.service.bootstrap.factory import _build_sandbox_manager

_MCP_IMAGE = "registry.example/zeroth-mcp@sha256:" + "b" * 64


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


@pytest.mark.parametrize(
    "escape_hatch",
    ["allow_untrusted_local_development", "allow_unisolated_mcp_development"],
)
def test_production_rejects_development_only_execution_escape_hatches(escape_hatch) -> None:
    with pytest.raises(ValueError, match="development-only"):
        ZerothSettings(
            deployment_mode="production",
            auth={"browser_session_secret": "s" * 32},
            sandbox=SandboxSettings(**{escape_hatch: True}),
        )


def test_production_requires_shared_browser_session_secret() -> None:
    with pytest.raises(ValueError, match="shared browser_session_secret"):
        ZerothSettings(deployment_mode="production")


def test_service_refuses_implicit_ephemeral_browser_session_signing(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "auth", AuthSettings())
    bootstrap = SimpleNamespace(
        authenticator=ServiceAuthenticator(),
        langgraph_gateway_compatibility=None,
        langgraph_gateway_proxy=object(),
        langgraph_gateway_websocket_handler=object(),
        regulus_client=None,
    )

    with pytest.raises(RuntimeError, match="browser session secret"):
        create_app(bootstrap)


def test_production_rejects_ephemeral_browser_session_development_flag() -> None:
    with pytest.raises(ValueError, match="development-only"):
        ZerothSettings(
            deployment_mode="production",
            auth={
                "browser_session_secret": "s" * 32,
                "allow_ephemeral_browser_session_secret_development": True,
            },
        )


def test_mcp_isolation_settings_require_a_digest_pinned_image() -> None:
    with pytest.raises(ValueError, match="digest-pinned"):
        SandboxSettings(mcp_isolation_image="registry.example/zeroth-mcp:latest")


def test_mcp_isolation_settings_preserve_operator_owned_profile() -> None:
    settings = SandboxSettings(
        mcp_isolation_image=_MCP_IMAGE,
        mcp_isolation_network="zeroth-mcp-egress",
        mcp_isolation_allowed_environment_keys=("API_TOKEN",),
    )

    assert settings.mcp_isolation_image == _MCP_IMAGE
    assert settings.mcp_isolation_network == "zeroth-mcp-egress"
    assert settings.mcp_isolation_allowed_environment_keys == ("API_TOKEN",)


def test_platform_budget_default_is_fail_closed() -> None:
    assert ZerothSettings().regulus.fail_closed is True


def test_production_rejects_fail_open_budget_configuration() -> None:
    with pytest.raises(ValueError, match="fail-closed budget"):
        ZerothSettings(
            deployment_mode="production",
            auth={"browser_session_secret": "s" * 32},
            regulus={"fail_closed": False},
        )


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


@pytest.mark.asyncio
async def test_factory_wires_digest_pinned_mcp_isolator_into_orchestrator(
    sqlite_db, monkeypatch
) -> None:
    from tests.service.helpers import agent_graph, deploy_service

    monkeypatch.setattr(
        get_settings(),
        "sandbox",
        SandboxSettings(
            mcp_isolation_image=_MCP_IMAGE,
            mcp_isolation_network="zeroth-mcp-egress",
        ),
    )

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="mcp-isolation-wiring"))

    isolator = service.orchestrator.mcp_process_isolator
    assert isolator is not None
    assert isolator.config.image == _MCP_IMAGE
    assert isolator.config.network == "zeroth-mcp-egress"

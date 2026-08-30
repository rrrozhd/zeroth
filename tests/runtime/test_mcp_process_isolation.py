"""Security contract for registered MCP process isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.mcp import RegisteredMCPServerConfig, tool_schema_hash
from zeroth.runtime.agents.mcp_isolation import (
    MCPDockerIsolationConfig,
    MCPEnvironmentDeniedError,
    MCPProcessIsolator,
)
from zeroth.runtime.agents.mcp_pool import MCPSessionPool
from zeroth.runtime.agents.tools import ToolAttachmentManifest

_CAPABILITIES = {Capability.EXTERNAL_API_CALL, Capability.PROCESS_SPAWN}
_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}
_PIN = tool_schema_hash("echo", "Echo", _SCHEMA)
_IMAGE = "registry.example/zeroth-mcp@sha256:" + "a" * 64


def _registration(*, env: dict[str, str] | None = None) -> RegisteredMCPServerConfig:
    return RegisteredMCPServerConfig(
        name="echo",
        command="python",
        args=["-m", "mcp_echo"],
        env=env,
        grants=sorted(_CAPABILITIES, key=lambda item: item.value),
    )


def test_isolation_requires_an_image_pinned_by_digest() -> None:
    with pytest.raises(ValueError, match="digest-pinned"):
        MCPDockerIsolationConfig(image="registry.example/zeroth-mcp:latest")


def test_isolation_accepts_a_local_immutable_image_id() -> None:
    config = MCPDockerIsolationConfig(image="sha256:" + "e" * 64)

    assert config.image == "sha256:" + "e" * 64


def test_isolator_builds_a_hardened_argv_without_environment_values() -> None:
    isolator = MCPProcessIsolator(
        MCPDockerIsolationConfig(
            image=_IMAGE,
            docker_binary="/usr/bin/docker",
            network="zeroth-mcp-egress",
            allowed_environment_keys=("API_TOKEN",),
        )
    )

    isolated = isolator.isolate(_registration(env={"API_TOKEN": "credential-value"}))

    assert isolated.command == "/usr/bin/docker"
    assert isolated.args == [
        "run",
        "--rm",
        "-i",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--user",
        "65534:65534",
        "--cpus",
        "1.0",
        "--memory",
        "256m",
        "--pids-limit",
        "64",
        "--network",
        "zeroth-mcp-egress",
        "--env",
        "API_TOKEN",
        _IMAGE,
        "python",
        "-m",
        "mcp_echo",
    ]
    assert isolated.env == {"API_TOKEN": "credential-value"}
    assert "credential-value" not in " ".join(isolated.args)


def test_isolator_rejects_unapproved_environment_before_docker() -> None:
    isolator = MCPProcessIsolator(
        MCPDockerIsolationConfig(image=_IMAGE, allowed_environment_keys=("SAFE",))
    )

    with pytest.raises(MCPEnvironmentDeniedError, match="SECRET"):
        isolator.isolate(_registration(env={"SECRET": "must-not-cross"}))


@pytest.mark.parametrize(
    "key",
    ["DOCKER_HOST", "DOCKER_CONTEXT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"],
)
def test_isolation_forbids_environment_that_can_retarget_docker(key: str) -> None:
    with pytest.raises(ValueError, match="Docker control environment"):
        MCPDockerIsolationConfig(image=_IMAGE, allowed_environment_keys=(key,))


@pytest.mark.asyncio
async def test_pool_dispatches_through_isolated_config_without_development_escape_hatch() -> None:
    async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
        return _registration(env={"API_TOKEN": "credential-value"}) if server_ref == "echo" else None

    isolator = MCPProcessIsolator(
        MCPDockerIsolationConfig(image=_IMAGE, allowed_environment_keys=("API_TOKEN",))
    )
    observed = []

    async def start(manager):  # noqa: ANN001
        observed.extend(manager._configs)
        return [
            ToolAttachmentManifest(
                alias="echo",
                executable_unit_ref="mcp://echo/echo",
                description="Echo",
                parameters_schema=_SCHEMA,
            )
        ]

    pool = MCPSessionPool(resolve, process_isolator=isolator)
    with (
        patch("zeroth.runtime.agents.mcp.MCPClientManager.start", new=start),
        patch(
            "zeroth.runtime.agents.mcp.MCPClientManager.call_tool",
            new=AsyncMock(return_value="ok"),
        ),
        patch("zeroth.runtime.agents.mcp.MCPClientManager.stop", new=AsyncMock()),
    ):
        result = await pool.call(
            server_ref="echo",
            tool_name="echo",
            arguments={"text": "hello"},
            agent_node_id="agent",
            tool_node_id="mcp_echo",
            declared_capabilities=_CAPABILITIES,
            effective_capabilities=_CAPABILITIES,
            pinned_hash=_PIN,
        )
        await pool.stop()

    assert result == "ok"
    assert observed[0].command == "docker"
    assert observed[0].args[-4:] == [_IMAGE, "python", "-m", "mcp_echo"]

"""Real stdio transport proof through a digest-pinned Docker MCP sandbox."""

from __future__ import annotations

import os

import pytest

from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.mcp import MCPClientManager, RegisteredMCPServerConfig
from zeroth.runtime.agents.mcp_isolation import MCPDockerIsolationConfig, MCPProcessIsolator


@pytest.mark.asyncio
async def test_digest_pinned_container_handshakes_calls_and_stops() -> None:
    image = os.environ.get("ZEROTH_TEST_MCP_IMAGE")
    if image is None:
        pytest.skip("ZEROTH_TEST_MCP_IMAGE is not configured")
    assert image.startswith("sha256:")

    registration = RegisteredMCPServerConfig(
        name="isolated-echo",
        command="python",
        args=["/opt/zeroth/echo_server.py"],
        grants=[Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL],
    )
    transport = MCPProcessIsolator(MCPDockerIsolationConfig(image=image)).isolate(
        registration
    )
    manager = MCPClientManager([transport])
    try:
        manifests = await manager.start()
        assert {manifest.alias for manifest in manifests} == {"add", "echo"}
        assert await manager.call_tool("echo", {"text": "isolated"}) == "isolated"
    finally:
        await manager.stop()

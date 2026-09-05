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
    transport = MCPProcessIsolator(MCPDockerIsolationConfig(image=image)).isolate(registration)
    manager = MCPClientManager([transport])
    try:
        manifests = await manager.start()
        assert {manifest.alias for manifest in manifests} == {"add", "echo"}
        assert await manager.call_tool("echo", {"text": "isolated"}) == "isolated"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_startup_timeout_removes_mcp_container_that_ignores_termination() -> None:
    import json
    import subprocess
    from uuid import uuid4

    from zeroth.runtime.agents.mcp import MCPTimeoutError

    image = os.environ.get("ZEROTH_TEST_MCP_IMAGE")
    if image is None:
        pytest.skip("ZEROTH_TEST_MCP_IMAGE is not configured")
    body = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        f"\n# owned-mcp-test-{uuid4().hex}"
    )

    def owned_containers() -> list[str]:
        ids = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"ancestor={image}"], text=True
        ).split()
        owned = []
        for container_id in ids:
            config = json.loads(
                subprocess.check_output(["docker", "inspect", container_id], text=True)
            )[0]["Config"]
            if config["Image"] == image and config["Cmd"] == ["python", "-c", body]:
                owned.append(container_id)
        return owned

    registration = RegisteredMCPServerConfig(
        name="unresponsive",
        command="python",
        args=["-c", body],
        grants=[Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL],
    )
    transport = MCPProcessIsolator(MCPDockerIsolationConfig(image=image)).isolate(registration)
    manager = MCPClientManager([transport], startup_timeout_seconds=1)
    try:
        try:
            with pytest.raises(MCPTimeoutError):
                await manager.start()
        finally:
            await manager.stop()
        assert not owned_containers(), "MCP shutdown returned with its workload still running"
    finally:
        for container_id in owned_containers():
            subprocess.run(
                ["docker", "rm", "--force", container_id], check=True, capture_output=True
            )

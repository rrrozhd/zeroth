"""Tests for MCP integration wiring in AgentConfig and AgentRunner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from zeroth.agent_runtime.mcp import MCPClientManager, MCPServerConfig
from zeroth.agent_runtime.models import AgentConfig
from zeroth.agent_runtime.provider import ProviderResponse
from zeroth.agent_runtime.runner import AgentRunner
from zeroth.agent_runtime.tools import ToolAttachmentManifest


class SimpleInput(BaseModel):
    text: str


class SimpleOutput(BaseModel):
    result: str


def _make_config(**overrides) -> AgentConfig:
    defaults = {
        "name": "test-agent",
        "instruction": "You are a test agent.",
        "model_name": "test-model",
        "input_model": SimpleInput,
        "output_model": SimpleOutput,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_provider_response(content: str = '{"result": "ok"}'):
    return ProviderResponse(content=content)


class TestAgentConfigMCPServers:
    def test_default_empty_list(self):
        config = _make_config()
        assert config.mcp_servers == []

    def test_accepts_mcp_server_configs(self):
        servers = [
            MCPServerConfig(name="web", command="python", args=["server.py"]),
            MCPServerConfig(name="db", command="node", args=["db-server.js"]),
        ]
        config = _make_config(mcp_servers=servers)
        assert len(config.mcp_servers) == 2
        assert config.mcp_servers[0].name == "web"
        assert config.mcp_servers[1].name == "db"


class TestAgentRunnerMCPWiring:
    def test_no_mcp_manager_without_servers(self):
        config = _make_config()
        provider = AsyncMock()
        runner = AgentRunner(config, provider)
        assert runner._mcp_manager is None

    @pytest.mark.asyncio
    async def test_run_without_mcp_works_normally(self):
        """Agents without mcp_servers work identically to before."""
        config = _make_config()
        provider = AsyncMock()
        response = _make_provider_response()
        provider.ainvoke = AsyncMock(return_value=response)

        runner = AgentRunner(config, provider)
        with patch.object(runner, "provider") as mock_prov:
            mock_prov.ainvoke = AsyncMock(return_value=response)
            with patch(
                "zeroth.agent_runtime.runner.run_provider_with_timeout",
                new=AsyncMock(return_value=response),
            ):
                result = await runner.run(SimpleInput(text="hello"))

        assert result.output_data == {"result": "ok"}
        assert runner._mcp_manager is None

    @pytest.mark.asyncio
    async def test_start_mcp_servers_discovers_and_registers_tools(self):
        """_start_mcp_servers creates MCPClientManager, discovers tools, registers them."""
        servers = [MCPServerConfig(name="test", command="echo", args=[])]
        config = _make_config(mcp_servers=servers)
        provider = AsyncMock()
        runner = AgentRunner(config, provider)

        mock_manifest = ToolAttachmentManifest(
            alias="mcp_tool",
            executable_unit_ref="mcp://test/mcp_tool",
            description="A test MCP tool",
        )

        with patch.object(
            MCPClientManager, "start", new=AsyncMock(return_value=[mock_manifest])
        ):
            await runner._start_mcp_servers()

        assert runner._mcp_manager is not None
        assert runner.tool_bridge.registry.has("mcp_tool")
        assert "mcp_tool" in runner.config.declared_tool_refs

    @pytest.mark.asyncio
    async def test_stop_mcp_servers_cleans_up(self):
        """_stop_mcp_servers calls stop on the manager and resets to None."""
        config = _make_config()
        provider = AsyncMock()
        runner = AgentRunner(config, provider)

        mock_manager = AsyncMock(spec=MCPClientManager)
        runner._mcp_manager = mock_manager

        await runner._stop_mcp_servers()

        mock_manager.stop.assert_called_once()
        assert runner._mcp_manager is None

    @pytest.mark.asyncio
    async def test_stop_mcp_servers_noop_when_none(self):
        """_stop_mcp_servers is safe to call when no manager exists."""
        config = _make_config()
        provider = AsyncMock()
        runner = AgentRunner(config, provider)

        # Should not raise
        await runner._stop_mcp_servers()
        assert runner._mcp_manager is None

    @pytest.mark.asyncio
    async def test_mcp_cleanup_on_error(self):
        """MCP servers are stopped even if run() raises an exception."""
        servers = [MCPServerConfig(name="test", command="echo", args=[])]
        config = _make_config(mcp_servers=servers)
        provider = AsyncMock()
        runner = AgentRunner(config, provider)

        with (
            patch.object(
                MCPClientManager, "start", new=AsyncMock(return_value=[])
            ),
            patch.object(
                MCPClientManager, "stop", new=AsyncMock()
            ) as mock_stop,
            patch(
                "zeroth.agent_runtime.runner.run_provider_with_timeout",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            with pytest.raises(Exception):
                await runner.run(SimpleInput(text="hello"))

            mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_tool_call_routes_through_manager(self):
        """MCP tool calls (mcp:// refs) route through MCPClientManager.call_tool()."""
        config = _make_config(
            tool_attachments=[
                ToolAttachmentManifest(
                    alias="mcp_search",
                    executable_unit_ref="mcp://web/search",
                    description="Search",
                ),
            ],
            max_tool_calls=2,
        )
        provider = AsyncMock()
        runner = AgentRunner(config, provider)

        # Set up mock MCP manager
        mock_manager = AsyncMock(spec=MCPClientManager)
        mock_manager.call_tool = AsyncMock(return_value="search result")
        runner._mcp_manager = mock_manager

        # Create a response with a tool call
        tool_response = MagicMock()
        tool_response.tool_calls = [
            {"id": "tc1", "name": "mcp_search", "args": {"q": "test"}}
        ]
        tool_response.raw = None
        tool_response.content = None

        # Final response after tool call
        final_response = _make_provider_response()

        with patch(
            "zeroth.agent_runtime.runner.run_provider_with_timeout",
            new=AsyncMock(return_value=final_response),
        ):
            result_response, result_messages, tool_audits = await runner._resolve_tool_calls(
                response=tool_response,
                messages=[],
                provider_timeout_seconds=30.0,
                approval_required_for_side_effects=False,
            )

        mock_manager.call_tool.assert_called_once_with("mcp_search", {"q": "test"})

    @pytest.mark.asyncio
    async def test_non_mcp_tool_call_uses_executor(self):
        """Non-MCP tool calls (no mcp:// prefix) still use tool_executor."""
        config = _make_config(
            tool_attachments=[
                ToolAttachmentManifest(
                    alias="local_tool",
                    executable_unit_ref="local://my_tool",
                    description="Local tool",
                ),
            ],
            max_tool_calls=2,
        )
        mock_executor = MagicMock(return_value={"value": "local result"})
        provider = AsyncMock()
        runner = AgentRunner(config, provider, tool_executor=mock_executor)

        tool_response = MagicMock()
        tool_response.tool_calls = [
            {"id": "tc1", "name": "local_tool", "args": {"x": 1}}
        ]
        tool_response.raw = None
        tool_response.content = None

        final_response = _make_provider_response()

        with patch(
            "zeroth.agent_runtime.runner.run_provider_with_timeout",
            new=AsyncMock(return_value=final_response),
        ):
            await runner._resolve_tool_calls(
                response=tool_response,
                messages=[],
                provider_timeout_seconds=30.0,
                approval_required_for_side_effects=False,
            )

        mock_executor.assert_called_once()

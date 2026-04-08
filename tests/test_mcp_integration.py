"""Tests for MCP (Model Context Protocol) client integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.agent_runtime.mcp import MCPClientManager, MCPServerConfig


class TestMCPServerConfig:
    def test_create_with_all_fields(self):
        config = MCPServerConfig(
            name="test-server",
            command="python",
            args=["server.py", "--port", "8080"],
            env={"API_KEY": "secret"},
        )
        assert config.name == "test-server"
        assert config.command == "python"
        assert config.args == ["server.py", "--port", "8080"]
        assert config.env == {"API_KEY": "secret"}

    def test_defaults(self):
        config = MCPServerConfig(name="minimal", command="node")
        assert config.args == []
        assert config.env is None

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            MCPServerConfig(name="test", command="python", unknown_field="value")


class TestMCPClientManagerInit:
    def test_stores_configs(self):
        configs = [
            MCPServerConfig(name="a", command="python"),
            MCPServerConfig(name="b", command="node"),
        ]
        manager = MCPClientManager(configs)
        assert manager._configs == configs
        assert manager._sessions == {}
        assert manager._tool_map == {}

    def test_empty_configs(self):
        manager = MCPClientManager([])
        assert manager._configs == []


class TestMCPClientManagerStart:
    @pytest.fixture
    def mock_mcp(self):
        """Set up mocked MCP SDK components."""
        mock_tool_1 = MagicMock()
        mock_tool_1.name = "search"
        mock_tool_1.description = "Search the web"
        mock_tool_1.inputSchema = {"type": "object", "properties": {"q": {"type": "string"}}}

        mock_tool_2 = MagicMock()
        mock_tool_2.name = "fetch"
        mock_tool_2.description = "Fetch a URL"
        mock_tool_2.inputSchema = {"type": "object", "properties": {"url": {"type": "string"}}}

        mock_list_tools_result = MagicMock()
        mock_list_tools_result.tools = [mock_tool_1, mock_tool_2]

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_list_tools_result)

        mock_transport = (MagicMock(), MagicMock())

        return {
            "session": mock_session,
            "transport": mock_transport,
            "tools": [mock_tool_1, mock_tool_2],
            "list_tools_result": mock_list_tools_result,
        }

    @pytest.mark.asyncio
    async def test_start_discovers_tools(self, mock_mcp):
        configs = [MCPServerConfig(name="web", command="python", args=["server.py"])]
        manager = MCPClientManager(configs)

        with (
            patch("mcp.client.stdio.stdio_client") as mock_stdio,
            patch("mcp.ClientSession") as mock_session_cls,
        ):
            # Make the async context managers work
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=mock_mcp["transport"])
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_mcp["session"]
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            manifests = await manager.start()

        assert len(manifests) == 2
        assert manifests[0].alias == "search"
        assert manifests[0].executable_unit_ref == "mcp://web/search"
        assert manifests[0].description == "Search the web"
        assert manifests[0].parameters_schema == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }
        assert manifests[1].alias == "fetch"
        assert manifests[1].executable_unit_ref == "mcp://web/fetch"

    @pytest.mark.asyncio
    async def test_tool_name_collision_namespacing(self):
        """When two servers expose a tool with the same name, the second gets namespaced."""
        configs = [
            MCPServerConfig(name="server_a", command="python"),
            MCPServerConfig(name="server_b", command="node"),
        ]
        manager = MCPClientManager(configs)

        # Both servers have a tool named "search"
        mock_tool_a = MagicMock()
        mock_tool_a.name = "search"
        mock_tool_a.description = "Search A"
        mock_tool_a.inputSchema = None

        mock_tool_b = MagicMock()
        mock_tool_b.name = "search"
        mock_tool_b.description = "Search B"
        mock_tool_b.inputSchema = None

        result_a = MagicMock()
        result_a.tools = [mock_tool_a]
        result_b = MagicMock()
        result_b.tools = [mock_tool_b]

        session_a = AsyncMock()
        session_a.initialize = AsyncMock()
        session_a.list_tools = AsyncMock(return_value=result_a)

        session_b = AsyncMock()
        session_b.initialize = AsyncMock()
        session_b.list_tools = AsyncMock(return_value=result_b)

        transport_a = (MagicMock(), MagicMock())
        transport_b = (MagicMock(), MagicMock())

        # Create separate context managers for each call
        cm_stdio_a = AsyncMock()
        cm_stdio_a.__aenter__ = AsyncMock(return_value=transport_a)
        cm_stdio_a.__aexit__ = AsyncMock(return_value=False)

        cm_stdio_b = AsyncMock()
        cm_stdio_b.__aenter__ = AsyncMock(return_value=transport_b)
        cm_stdio_b.__aexit__ = AsyncMock(return_value=False)

        cm_session_a = AsyncMock()
        cm_session_a.__aenter__ = AsyncMock(return_value=session_a)
        cm_session_a.__aexit__ = AsyncMock(return_value=False)

        cm_session_b = AsyncMock()
        cm_session_b.__aenter__ = AsyncMock(return_value=session_b)
        cm_session_b.__aexit__ = AsyncMock(return_value=False)

        stdio_calls = iter([cm_stdio_a, cm_stdio_b])
        session_calls = iter([cm_session_a, cm_session_b])

        with (
            patch("mcp.client.stdio.stdio_client", side_effect=lambda params: next(stdio_calls)),
            patch("mcp.ClientSession", side_effect=lambda r, w: next(session_calls)),
        ):
            manifests = await manager.start()

        assert len(manifests) == 2
        assert manifests[0].alias == "search"
        assert manifests[0].executable_unit_ref == "mcp://server_a/search"
        assert manifests[1].alias == "server_b__search"
        assert manifests[1].executable_unit_ref == "mcp://server_b/search"


class TestMCPClientManagerCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_routes_to_correct_session(self):
        manager = MCPClientManager([])
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_content_block = MagicMock()
        mock_content_block.text = "result text"
        mock_result.content = [mock_content_block]
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        manager._sessions["web"] = mock_session
        manager._tool_map["search"] = "web"

        result = await manager.call_tool("search", {"q": "test"})
        mock_session.call_tool.assert_called_once_with("search", {"q": "test"})
        assert result == "result text"

    @pytest.mark.asyncio
    async def test_call_tool_namespaced_extracts_original_name(self):
        manager = MCPClientManager([])
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_content_block = MagicMock()
        mock_content_block.text = "ok"
        mock_result.content = [mock_content_block]
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        manager._sessions["server_b"] = mock_session
        manager._tool_map["server_b__search"] = "server_b"

        result = await manager.call_tool("server_b__search", {"q": "hello"})
        # Should call with the original name "search", not "server_b__search"
        mock_session.call_tool.assert_called_once_with("search", {"q": "hello"})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_call_tool_unknown_raises_key_error(self):
        manager = MCPClientManager([])
        with pytest.raises(KeyError, match="MCP tool not found: nonexistent"):
            await manager.call_tool("nonexistent", {})


class TestMCPClientManagerStop:
    @pytest.mark.asyncio
    async def test_stop_closes_exit_stack(self):
        manager = MCPClientManager([])
        manager._exit_stack = AsyncMock()
        manager._exit_stack.aclose = AsyncMock()

        await manager.stop()
        manager._exit_stack.aclose.assert_called_once()

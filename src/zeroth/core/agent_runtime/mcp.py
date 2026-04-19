"""MCP (Model Context Protocol) client integration for agent tool discovery.

Manages connections to MCP servers, discovers their tools, and routes
tool calls through the appropriate MCP client session.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.agent_runtime.tools import ToolAttachmentManifest

logger = logging.getLogger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for connecting to an MCP server via stdio transport."""

    model_config = ConfigDict(extra="forbid")
    name: str
    command: str  # e.g. "python", "node", "npx"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None


class MCPClientManager:
    """Manages MCP server connections and tool discovery.

    Connects to one or more MCP servers, discovers their tools,
    and provides a dispatch mechanism for calling tools on the
    correct server. Uses AsyncExitStack for lifecycle management.
    """

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self._configs = configs
        self._sessions: dict[str, Any] = {}  # server_name -> ClientSession
        self._tool_map: dict[str, str] = {}  # tool_name -> server_name
        self._exit_stack = AsyncExitStack()

    async def start(self) -> list[ToolAttachmentManifest]:
        """Connect to all configured MCP servers and discover tools.

        Returns a list of ToolAttachmentManifest entries, one per
        discovered tool across all servers. Raises on connection failure.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        manifests: list[ToolAttachmentManifest] = []
        for config in self._configs:
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env,
            )
            transport = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(transport[0], transport[1])
            )
            await session.initialize()
            self._sessions[config.name] = session

            response = await session.list_tools()
            for tool in response.tools:
                tool_name = tool.name
                if tool_name in self._tool_map:
                    # Namespace collision -- prefix with server name
                    tool_name = f"{config.name}__{tool.name}"
                    logger.warning(
                        "MCP tool name collision: %s already registered, "
                        "using namespaced name %s",
                        tool.name,
                        tool_name,
                    )
                self._tool_map[tool_name] = config.name
                manifests.append(
                    ToolAttachmentManifest(
                        alias=tool_name,
                        executable_unit_ref=f"mcp://{config.name}/{tool.name}",
                        description=tool.description or "",
                        parameters_schema=(
                            tool.inputSchema if hasattr(tool, "inputSchema") else None
                        ),
                    )
                )
            logger.info(
                "MCP server %s: discovered %d tools",
                config.name,
                len(response.tools),
            )
        return manifests

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on its MCP server and return the result.

        Routes to the correct server session based on tool_name.
        Raises KeyError if tool_name is not registered.
        """
        server_name = self._tool_map.get(tool_name)
        if server_name is None:
            raise KeyError(f"MCP tool not found: {tool_name}")
        session = self._sessions[server_name]
        # Extract original tool name from namespaced version
        original_name = tool_name
        if tool_name.startswith(f"{server_name}__"):
            original_name = tool_name[len(f"{server_name}__") :]
        result = await session.call_tool(original_name, arguments)
        # Extract content from CallToolResult
        if hasattr(result, "content") and result.content:
            # MCP returns list of content blocks; concatenate text blocks
            texts = []
            for block in result.content:
                if hasattr(block, "text"):
                    texts.append(block.text)
            return "\n".join(texts) if texts else str(result.content)
        return str(result)

    async def stop(self) -> None:
        """Close all MCP server connections and clean up resources."""
        await self._exit_stack.aclose()

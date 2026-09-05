"""MCP (Model Context Protocol) client integration for agent tool discovery.

Manages connections to MCP servers, discovers their tools, and routes
tool calls through the appropriate MCP client session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents._mcp_docker_transport import (
    DockerStdioWorkload as _DockerStdioWorkload,
)
from zeroth.runtime.agents._mcp_docker_transport import (
    owned_docker_stdio as _owned_docker_stdio,
)
from zeroth.runtime.agents.tools import ToolAttachmentManifest

logger = logging.getLogger(__name__)

#: Deadline for an MCP handshake or tool call, in seconds.
#:
#: Startup runs *before* the agent's own ``timeout_seconds`` covers anything, so
#: a server that connects and then never answers used to hang the whole run with
#: no deadline anywhere in the module. These are the module's own bounds.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0

#: The capability floor every route to an MCP server must clear.
#:
#: Reaching a server spawns a subprocess that then calls out to an external
#: service, so the pair is not a policy choice -- it is what the mechanism does.
#: Four sites used to spell it as their own literal (publish validation twice,
#: the session pool, and the importer that writes an ``mcp_tool`` node's
#: bindings), which is three chances for the floor to drift apart from itself
#: and for publish to accept a node the runtime then denies. It lives here
#: because this module already owns ``Capability`` and sits below every one of
#: those callers, so importing it adds no edge that was not already there.
MCP_REQUIRED_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL}
)


def tool_schema_hash(name: str, description: str, input_schema: Any) -> str:
    """The pin for one discovered MCP tool.

    Canonicalisation is the whole contract here. Two runs of the same server
    can serialise an identical schema with different key ordering or spacing;
    if that reached the digest, the fail-closed drift check would fire on an
    unchanged server and every restart would look like tampering. Sorting keys
    and fixing separators makes the digest a function of the schema's meaning
    rather than of one serialiser's habits.

    ``description`` is inside the digest deliberately: it is model-visible text
    from an external process, so a server that silently rewrites it has changed
    what the agent was pinned to, even though the callable signature is intact.
    """
    canonical = json.dumps(
        {"name": name, "description": description, "input_schema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MCPSchemaDriftError(RuntimeError):
    """A server's live tool no longer matches what the graph pinned at import."""

    def __init__(self, server_name: str, tool_name: str, pinned: str, live: str) -> None:
        super().__init__(
            f"MCP tool {tool_name!r} on server {server_name!r} drifted from its pinned "
            f"schema (pinned {pinned[:12]}..., live {live[:12]}...); re-import to accept"
        )
        self.server_name = server_name
        self.tool_name = tool_name
        self.pinned = pinned
        self.live = live


class MCPTimeoutError(TimeoutError):
    """An MCP server did not answer within its deadline."""

    def __init__(self, operation: str, server_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"MCP server {server_name!r} did not answer {operation} within {timeout_seconds}s"
        )
        self.operation = operation
        self.server_name = server_name
        self.timeout_seconds = timeout_seconds


class MCPServerConfig(BaseModel):
    """Configuration for connecting to an MCP server via stdio transport."""

    model_config = ConfigDict(extra="forbid")
    name: str
    command: str  # e.g. "python", "node", "npx"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    _docker_workload: _DockerStdioWorkload | None = PrivateAttr(default=None)


class RegisteredMCPServerConfig(MCPServerConfig):
    """A server resolved from the operator registry, carrying its ceiling.

    Separate from :class:`MCPServerConfig` rather than a field on it, because
    that class is part of the immutable legacy surface
    (``tests/architecture/test_library_surface.py`` pins its signature against
    ``backend_surface_legacy.json``) -- widening it would break a contract the
    repository declares unchangeable. The split says something true anyway:
    ``MCPServerConfig`` is transport (what to spawn), while ``grants`` is
    governance (what the spawned thing may be used for), and only the registry
    path has the latter. The deprecated inline ``agent.mcp_servers`` path
    coerces author dicts into the BASE class, so ``extra="forbid"`` now refuses
    an author-written ``grants`` outright instead of relying on a hand-written
    guard to strip it.
    """

    #: The operator's assertion of what this server may do -- the ceiling a
    #: referencing node's ``capability_bindings`` must stay within, checked at
    #: publish. Without it the registry would only rename the problem it exists
    #: to solve: capability_bindings are author-declared (see PolicyGuard.evaluate),
    #: so an author who wants a capability simply writes it. This list is the one
    #: side of that check the author cannot edit.
    #:
    #: Empty means "asserted nothing", which denies every referencing node rather
    #: than permitting every one -- an unfilled ceiling must not read as no ceiling.
    grants: list[Capability] = Field(default_factory=list)


class MCPClientManager:
    """Manages MCP server connections and tool discovery.

    Connects to one or more MCP servers, discovers their tools,
    and provides a dispatch mechanism for calling tools on the
    correct server. Uses AsyncExitStack for lifecycle management.
    """

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._configs = configs
        self._sessions: dict[str, Any] = {}  # server_name -> ClientSession
        self._tool_map: dict[str, str] = {}  # tool_name -> server_name
        self._exit_stack = AsyncExitStack()
        self._startup_timeout_seconds = startup_timeout_seconds
        self._call_timeout_seconds = call_timeout_seconds

    async def _deadline(self, awaitable: Any, operation: str, server_name: str) -> Any:
        """Await *awaitable* under the deadline that fits *operation*."""
        timeout = (
            self._call_timeout_seconds
            if operation == "call_tool"
            else self._startup_timeout_seconds
        )
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as exc:
            raise MCPTimeoutError(operation, server_name, timeout) from exc

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
            context = (
                stdio_client(params)
                if config._docker_workload is None
                else _owned_docker_stdio(config._docker_workload, params, stdio_client)
            )
            transport = await self._exit_stack.enter_async_context(context)
            session = await self._exit_stack.enter_async_context(
                ClientSession(transport[0], transport[1])
            )
            await self._deadline(session.initialize(), "initialize", config.name)
            self._sessions[config.name] = session

            response = await self._deadline(session.list_tools(), "list_tools", config.name)
            for tool in response.tools:
                tool_name = tool.name
                if tool_name in self._tool_map:
                    # Namespace collision -- prefix with server name
                    tool_name = f"{config.name}__{tool.name}"
                    logger.warning(
                        "MCP tool name collision: %s already registered, using namespaced name %s",
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
                        # WS-C: an MCP tool runs inside a spawned server process
                        # and reaches out to external services, so it carries the
                        # same pair the startup gate demands. There is no graph
                        # node behind an mcp:// ref to derive a finer set from.
                        required_capabilities=tuple(
                            sorted(MCP_REQUIRED_CAPABILITIES, key=lambda cap: cap.value)
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
        result = await self._deadline(
            session.call_tool(original_name, arguments), "call_tool", server_name
        )
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

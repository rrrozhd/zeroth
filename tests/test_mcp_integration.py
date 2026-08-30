"""Tests for MCP (Model Context Protocol) client integration."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from tests.conftest import content_capture
from zeroth.contracts.graph import (
    AgentNodeData,
    AgentToolBinding,
    Edge,
    Graph,
)
from zeroth.contracts.graph.models import AgentNode, MCPToolNode, MCPToolNodeData
from zeroth.contracts.registry import ContractRegistry
from zeroth.governance.audit import AuditRepository
from zeroth.governance.policy.models import Capability
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import DeterministicProviderAdapter, ProviderResponse
from zeroth.runtime.agents.factory import build_agent_runners
from zeroth.runtime.agents.mcp import (
    MCPClientManager,
    MCPServerConfig,
    RegisteredMCPServerConfig,
    tool_schema_hash,
)
from zeroth.runtime.agents.mcp_pool import MCPSessionPool
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import RunStatus


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
        """A bare ``pytest.raises(Exception)`` would not prove what it claims.

        ``ValidationError``, ``TypeError`` and a typo in this very test all
        satisfy it, so the assertion passed whether or not ``extra="forbid"`` was
        configured. Naming the exception is what ties it to the setting.
        """
        with pytest.raises(ValidationError):
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


# ---------------------------------------------------------------------------
# A pinned mcp_tool node, driven through RuntimeOrchestrator against the real
# echo server. Every other MCP test here mocks the SDK; this one does not.
# ---------------------------------------------------------------------------

_SPAWN = [Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL]
_FIXTURE = ["-m", "tests.runtime.mcp_fixtures.echo_server"]


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    echoed: str


def _echo_config() -> RegisteredMCPServerConfig:
    return RegisteredMCPServerConfig(
        name="echo",
        command=sys.executable,
        args=list(_FIXTURE),
        grants=list(_SPAWN),
    )


async def _resolve_echo(server_ref: str) -> RegisteredMCPServerConfig | None:
    return _echo_config() if server_ref == "echo" else None


async def _pin(tool_name: str) -> tuple[str, dict[str, Any], str]:
    """Read the live declaration and freeze it exactly as an import would.

    Taken from the server rather than hand-written so the digest is the one the
    pool will recompute at run time: a hand-written schema that merely *looks*
    right fails the drift check and the test would prove nothing about the
    pin -- or, worse, would pass while pinning a contract no server serves.
    """
    manager = MCPClientManager([_echo_config()])
    try:
        manifests = await manager.start()
    finally:
        await manager.stop()
    manifest = next(m for m in manifests if m.alias == tool_name)
    schema = dict(manifest.parameters_schema or {})
    return (
        manifest.description,
        schema,
        tool_schema_hash(manifest.alias, manifest.description, manifest.parameters_schema),
    )


def _pinned_graph(description: str, schema: dict[str, Any], schema_hash: str) -> Graph:
    return Graph(
        graph_id="graph-mcp",
        name="mcp",
        entry_step="agent",
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-mcp:v1",
                input_contract_ref="contract://echo-in",
                output_contract_ref="contract://echo-out",
                agent=AgentNodeData(
                    instruction="echo the text using your tool",
                    model_provider="provider://test",
                    tool_bindings=[
                        AgentToolBinding(
                            target_node_id="mcp_echo",
                            name="echo",
                            # What ``import_mcp_tools`` writes: the server's own
                            # description, verbatim, with no ``arguments``.
                            description=description,
                        )
                    ],
                ),
            ),
            MCPToolNode(
                node_id="mcp_echo",
                graph_version_ref="graph-mcp:v1",
                capability_bindings=[capability.value for capability in _SPAWN],
                mcp_tool=MCPToolNodeData(
                    server_ref="echo",
                    tool_name="echo",
                    description=description,
                    input_schema=schema,
                    schema_hash=schema_hash,
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="tool-1",
                source_node_id="agent",
                target_node_id="mcp_echo",
                kind="tool",
            )
        ],
    )


class _RecordingPool(MCPSessionPool):
    """The real pool, plus a note of what each call was asked to enforce.

    Subclassed rather than mocked so the gate, the spawn, the pin check and the
    transport all still run: the point is to read the arguments the dispatch
    seam actually passed, not to replace the thing that consumes them.
    """

    def __init__(
        self,
        resolver,
        *,
        allow_development_unisolated_processes: bool = False,
    ):
        super().__init__(
            resolver,
            allow_development_unisolated_processes=allow_development_unisolated_processes,
        )
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs):
        self.calls.append(dict(kwargs))
        return await super().call(**kwargs)


async def _run_pinned_graph(sqlite_db, responses, *, pool_holder: list[_RecordingPool]):
    """Drive one pinned ``mcp_tool`` node end to end; return (run, provider, audits)."""
    description, schema, schema_hash = await _pin("echo")
    graph = _pinned_graph(description, schema, schema_hash)

    contract_registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await contract_registry.register(EchoInput, name="contract://echo-in")
    await contract_registry.register(EchoOutput, name="contract://echo-out")

    provider = DeterministicProviderAdapter(responses)
    runners = await build_agent_runners(graph, contract_registry, provider=provider)

    def build_pool(
        resolver,
        *,
        allow_development_unisolated_processes: bool = False,
        process_isolator=None,
    ):
        pool = _RecordingPool(
            resolver,
            allow_development_unisolated_processes=allow_development_unisolated_processes,
        )
        pool_holder.append(pool)
        return pool

    orchestrator = RuntimeOrchestrator(
        audit_repository=content_capture(AuditRepository.for_default_compatibility(sqlite_db)),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=MagicMock(),
        mcp_server_resolver=_resolve_echo,
        allow_development_unisolated_mcp_processes=True,
    )
    with patch(
        "zeroth.runtime.orchestration.orchestrator.MCPSessionPool",
        side_effect=build_pool,
    ):
        run = await orchestrator.run_graph(graph, {"text": "hello"})
    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    return run, provider, graph, audits


class TestPinnedMCPToolNodeEndToEnd:
    """The one test that watches findings 2, 3, 5, 8, 9 and 11 at the same time.

    Everything below is observed through ``RuntimeOrchestrator`` driving a real
    ``mcp_tool`` node against ``tests/runtime/mcp_fixtures/echo_server.py``: the
    schema the provider was handed, the ceiling subject the pool was asked to
    enforce, the arguments that crossed the wire, and the audit record the run
    left behind. Unit tests can each show one of those; only this one shows
    that they line up.
    """

    async def test_a_pinned_tool_runs_and_leaves_an_honest_record(self, sqlite_db):
        pools: list[_RecordingPool] = []
        run, provider, graph, audits = await _run_pinned_graph(
            sqlite_db,
            [
                ProviderResponse(
                    content=None,
                    tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hello"}}],
                ),
                ProviderResponse(content='{"echoed": "hello"}'),
            ],
            pool_holder=pools,
        )

        assert run.status is RunStatus.COMPLETED
        assert run.final_output == {"echoed": "hello"}

        # Finding 3: the model is offered the PINNED contract. Before the fix
        # the manifest compiled from the binding's (empty) ``arguments``, so the
        # provider saw an object with no properties and
        # ``additionalProperties: false`` -- a tool a real model could not call.
        (tool,) = provider.requests[0].tools
        parameters = tool["function"]["parameters"]
        assert "text" in parameters["properties"], parameters
        assert parameters["required"] == ["text"]
        assert tool["function"]["name"] == "echo"

        # Findings 2/8: the ceiling's subject is the mcp_tool node, and the
        # dispatch seam passes both ids separately.
        (recorded,) = pools[0].calls
        assert recorded["tool_node_id"] == "mcp_echo"
        assert recorded["agent_node_id"] == "agent"
        assert recorded["declared_capabilities"] == set(_SPAWN)
        assert recorded["server_ref"] == "echo"
        assert recorded["pinned_hash"] == graph.nodes[1].mcp_tool.schema_hash
        # The arguments that crossed the wire are the model's, under the names
        # the pinned schema declares.
        assert recorded["arguments"] == {"text": "hello"}

        # Finding 9 / the reason mcp_tool is its own node kind: the weaker
        # delivery guarantee is visible in the durable record.
        (audit,) = audits
        (tool_call,) = audit.tool_calls
        assert tool_call.tool_ref == "node://mcp_echo"
        assert tool_call.error is None
        assert tool_call.operation_support == "at_least_once"
        assert tool_call.operation_residual_duplicate_risk is True

    async def test_a_transport_failure_still_carries_the_at_least_once_marker(self, sqlite_db):
        """The marker's whole reason to exist, driven through the real stack.

        The failure is injected one layer *below* the pool, at the transport,
        so the pool's own error typing and the runner's reading of it are both
        the real ones -- which is the part finding 9 was about.
        """
        pools: list[_RecordingPool] = []
        real_call_tool = MCPClientManager.call_tool

        async def broken_call_tool(self, name, arguments):
            raise ConnectionResetError("connection reset mid-call")

        assert real_call_tool is not broken_call_tool
        with patch.object(MCPClientManager, "call_tool", broken_call_tool):
            run, _provider, _graph, audits = await _run_pinned_graph(
                sqlite_db,
                [
                    ProviderResponse(
                        content=None,
                        tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hello"}}],
                    ),
                    ProviderResponse(content='{"echoed": "failed"}'),
                ],
                pool_holder=pools,
            )

        assert run.status is RunStatus.COMPLETED
        (audit,) = audits
        (tool_call,) = audit.tool_calls
        assert tool_call.error is not None, "the failure must be audited"
        # A failed MCP call is the residual in its purest form: the effect may
        # have landed and nobody can ask. Losing the marker here is losing it
        # exactly where it matters most.
        assert tool_call.operation_support == "at_least_once"
        assert tool_call.operation_residual_duplicate_risk is True

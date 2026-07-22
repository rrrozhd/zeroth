"""WS-C: tool required-capability derivation (factory) and grant validation."""

from __future__ import annotations

import pytest

from zeroth.runtime.agents.factory import tool_required_capabilities
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    Node,
    ToolArgument,
)
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.contracts.graph.validation_errors import ValidationCode
from zeroth.governance.policy import Capability

REF = "cap-graph@1"


def _tool_binding() -> AgentToolBinding:
    return AgentToolBinding(
        target_node_id="unit",
        name="run_unit",
        description="Run the unit tool.",
        arguments=[ToolArgument(name="x", type="string", description="in", required=True)],
    )


def _graph(*, agent_caps: list[str], unit_caps: list[str]) -> Graph:
    return Graph(
        graph_id="cap-graph",
        name="cap",
        version=1,
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref=REF,
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                capability_bindings=agent_caps,
                agent=AgentNodeData(
                    instruction="use tool",
                    model_provider="governai:test",
                    tool_bindings=[_tool_binding()],
                ),
            ),
            ExecutableUnitNode(
                node_id="unit",
                graph_version_ref=REF,
                display=DisplayMetadata(title="unit"),
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                capability_bindings=unit_caps,
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="native",
                    manifest_ref="eu://unit",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="tool-edge",
                source_node_id="agent",
                target_node_id="unit",
                kind="tool",
            ),
        ],
    )


def test_factory_derives_required_capabilities_from_target_unit() -> None:
    node_map: dict[str, Node] = {
        n.node_id: n
        for n in _graph(agent_caps=[], unit_caps=["network_write", "secret_access"]).nodes
    }
    caps = tool_required_capabilities(_tool_binding(), node_map)
    assert set(caps) == {Capability.NETWORK_WRITE, Capability.SECRET_ACCESS}


def test_factory_unions_binding_declared_capabilities() -> None:
    binding = AgentToolBinding(
        target_node_id="unit",
        name="run_unit",
        description="d",
        required_capabilities=[Capability.EXTERNAL_API_CALL],
    )
    node_map: dict[str, Node] = {
        n.node_id: n for n in _graph(agent_caps=[], unit_caps=["network_read"]).nodes
    }
    caps = tool_required_capabilities(binding, node_map)
    assert set(caps) == {Capability.EXTERNAL_API_CALL, Capability.NETWORK_READ}


@pytest.mark.asyncio
async def test_validation_rejects_insufficient_grant() -> None:
    graph = _graph(agent_caps=["network_read"], unit_caps=["network_write"])
    report = await GraphValidator().validate(graph)
    codes = [issue.code for issue in report.issues]
    assert ValidationCode.CAPABILITY_GRANT_INSUFFICIENT in codes


@pytest.mark.asyncio
async def test_validation_accepts_superset_grant() -> None:
    graph = _graph(agent_caps=["network_write", "secret_access"], unit_caps=["network_write"])
    report = await GraphValidator().validate(graph)
    codes = [issue.code for issue in report.issues]
    assert ValidationCode.CAPABILITY_GRANT_INSUFFICIENT not in codes


def _mcp_graph(*, agent_caps: list[str]) -> Graph:
    return Graph(
        graph_id="mcp-graph",
        name="mcp",
        version=1,
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="mcp-graph@1",
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                capability_bindings=agent_caps,
                agent=AgentNodeData(
                    instruction="use mcp",
                    model_provider="governai:test",
                    mcp_servers=[{"name": "files", "command": "npx", "args": ["mcp-files"]}],
                ),
            ),
        ],
        edges=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_caps", "expected_missing"),
    [
        ([], ["external_api_call", "process_spawn"]),
        (["process_spawn"], ["external_api_call"]),
        (["external_api_call"], ["process_spawn"]),
    ],
)
async def test_validation_rejects_mcp_agent_missing_capabilities(
    agent_caps: list[str], expected_missing: list[str]
) -> None:
    report = await GraphValidator().validate(_mcp_graph(agent_caps=agent_caps))
    issues = [i for i in report.issues if i.code == ValidationCode.MISSING_MCP_CAPABILITY]
    assert len(issues) == 1
    assert issues[0].details["missing_capabilities"] == expected_missing


@pytest.mark.asyncio
async def test_validation_accepts_mcp_agent_with_both_capabilities() -> None:
    report = await GraphValidator().validate(
        _mcp_graph(agent_caps=["process_spawn", "external_api_call"])
    )
    codes = [issue.code for issue in report.issues]
    assert ValidationCode.MISSING_MCP_CAPABILITY not in codes

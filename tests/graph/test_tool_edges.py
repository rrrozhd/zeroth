"""Tool edges and agent tool bindings: model behavior and publish validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    ToolArgument,
)
from zeroth.core.graph.serialization import deserialize_graph, serialize_graph
from zeroth.core.graph.validation import GraphValidator
from zeroth.core.graph.validation_errors import ValidationCode


def _agent_node(node_id: str = "agent", **agent_kwargs) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        agent=AgentNodeData(
            instruction="do the thing",
            model_provider="provider://test",
            **agent_kwargs,
        ),
    )


def _unit_node(node_id: str = "lookup") -> ExecutableUnitNode:
    return ExecutableUnitNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="eu://lookup",
            execution_mode="native",
        ),
    )


def _binding(target: str = "lookup", name: str = "lookup_value") -> AgentToolBinding:
    return AgentToolBinding(
        target_node_id=target,
        name=name,
        description="Look up a value",
        arguments=[ToolArgument(name="key", description="The key to look up")],
    )


def _graph(nodes, edges) -> Graph:
    return Graph(graph_id="g", name="g", entry_step=nodes[0].node_id, nodes=nodes, edges=edges)


def _tool_edge(source: str = "agent", target: str = "lookup", edge_id: str = "t1") -> Edge:
    return Edge(edge_id=edge_id, source_node_id=source, target_node_id=target, kind="tool")


class TestModels:
    def test_edge_kind_defaults_to_data(self) -> None:
        edge = Edge(edge_id="e", source_node_id="a", target_node_id="b")
        assert edge.kind == "data"

    def test_binding_compiles_parameters_schema(self) -> None:
        binding = AgentToolBinding(
            target_node_id="lookup",
            name="lookup_value",
            description="Look up a value",
            arguments=[
                ToolArgument(name="key", description="The key"),
                ToolArgument(name="limit", type="integer", description="Max hits", required=False),
            ],
        )
        assert binding.parameters_schema() == {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The key"},
                "limit": {"type": "integer", "description": "Max hits"},
            },
            "required": ["key"],
            "additionalProperties": False,
        }

    def test_binding_requires_name_description_and_argument_descriptions(self) -> None:
        with pytest.raises(ValidationError):
            AgentToolBinding(target_node_id="lookup", name="", description="d")
        with pytest.raises(ValidationError):
            AgentToolBinding(target_node_id="lookup", name="has spaces", description="d")
        with pytest.raises(ValidationError):
            AgentToolBinding(target_node_id="lookup", name="ok", description="")
        with pytest.raises(ValidationError):
            AgentToolBinding(
                target_node_id="lookup",
                name="ok",
                description="d",
                arguments=[ToolArgument(name="key", description="")],
            )

    def test_binding_rejects_duplicate_argument_names(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            AgentToolBinding(
                target_node_id="lookup",
                name="ok",
                description="d",
                arguments=[
                    ToolArgument(name="key", description="a"),
                    ToolArgument(name="key", description="b"),
                ],
            )

    def test_tool_edges_do_not_become_transitions(self) -> None:
        agent = _agent_node(tool_bindings=[_binding()])
        unit = _unit_node()
        graph = _graph([agent, unit], [_tool_edge()])
        spec = graph.to_governed_flow_spec()
        agent_step = next(step for step in spec.steps if step.name == "agent")
        # No data edges leave the agent: the tool edge must not turn into one.
        assert agent_step.transition.kind == "end"

    def test_tool_edges_round_trip_serialization(self) -> None:
        agent = _agent_node(tool_bindings=[_binding()], input_messages_key="messages")
        unit = _unit_node()
        graph = _graph([agent, unit], [_tool_edge()])
        restored = deserialize_graph(serialize_graph(graph))
        assert restored.edges[0].kind == "tool"
        restored_agent = next(n for n in restored.nodes if n.node_id == "agent")
        assert restored_agent.agent.tool_bindings[0].name == "lookup_value"
        assert restored_agent.agent.input_messages_key == "messages"


class TestValidation:
    async def _codes(self, graph: Graph) -> list[str]:
        report = await GraphValidator().validate(graph)
        return [issue.code for issue in report.issues]

    async def test_valid_tool_attachment_passes(self) -> None:
        agent = _agent_node(tool_bindings=[_binding()])
        graph = _graph([agent, _unit_node()], [_tool_edge()])
        codes = await self._codes(graph)
        assert ValidationCode.INVALID_TOOL_EDGE not in codes
        assert ValidationCode.INVALID_TOOL_BINDING not in codes

    async def test_tool_edge_source_must_be_agent(self) -> None:
        graph = _graph(
            [_agent_node(), _unit_node(), _unit_node("other")],
            [_tool_edge(source="other", target="lookup")],
        )
        assert ValidationCode.INVALID_TOOL_EDGE in await self._codes(graph)

    async def test_tool_edge_target_must_be_executable_unit(self) -> None:
        graph = _graph(
            [_agent_node(), _agent_node("agent2"), _unit_node()],
            [_tool_edge(target="agent2")],
        )
        assert ValidationCode.INVALID_TOOL_EDGE in await self._codes(graph)

    async def test_attached_tool_without_binding_is_an_error(self) -> None:
        graph = _graph([_agent_node(), _unit_node()], [_tool_edge()])
        assert ValidationCode.INVALID_TOOL_BINDING in await self._codes(graph)

    async def test_binding_without_tool_edge_is_an_error(self) -> None:
        agent = _agent_node(tool_bindings=[_binding()])
        graph = _graph([agent, _unit_node()], [])
        assert ValidationCode.INVALID_TOOL_BINDING in await self._codes(graph)

    async def test_duplicate_tool_names_are_an_error(self) -> None:
        agent = _agent_node(
            tool_bindings=[
                _binding(target="lookup", name="lookup_value"),
                _binding(target="lookup2", name="lookup_value"),
            ]
        )
        graph = _graph(
            [agent, _unit_node(), _unit_node("lookup2")],
            [_tool_edge(), _tool_edge(target="lookup2", edge_id="t2")],
        )
        assert ValidationCode.INVALID_TOOL_BINDING in await self._codes(graph)

    async def test_tool_edge_with_condition_is_an_error(self) -> None:
        from zeroth.core.graph.models import Condition

        agent = _agent_node(tool_bindings=[_binding()])
        edge = Edge(
            edge_id="t1",
            source_node_id="agent",
            target_node_id="lookup",
            kind="tool",
            condition=Condition(expression="payload.x > 1"),
        )
        graph = _graph([agent, _unit_node()], [edge])
        assert ValidationCode.INVALID_TOOL_EDGE in await self._codes(graph)

    async def test_tool_edges_do_not_create_unsafe_cycles(self) -> None:
        # unit -> agent (data) plus agent -> unit (tool) is not a cycle:
        # the tool edge is never traversed.
        agent = _agent_node(tool_bindings=[_binding()])
        unit = _unit_node()
        graph = _graph(
            [unit, agent],
            [
                Edge(edge_id="d1", source_node_id="lookup", target_node_id="agent"),
                _tool_edge(),
            ],
        )
        assert ValidationCode.UNSAFE_CYCLE not in await self._codes(graph)

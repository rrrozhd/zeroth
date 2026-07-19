"""Edge wiring, tool-edge structure, and tool attachment validation.

Tool *structure* (which node kinds a tool edge may join, what it may carry) is
a contract rule and lives here. Tool *capability grants* resolve refs against
the governance Capability enum, so they arrive through the same injected
collaborator the node validators use.
"""

from __future__ import annotations

from collections import defaultdict

from zeroth.contracts.graph.validation.capabilities import NullCapabilityChecks
from zeroth.contracts.graph.validation.edges import validate_edges
from zeroth.contracts.graph.validation.tools import validate_tool_attachments
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    Condition,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    Node,
)
from zeroth.core.graph.validation_errors import ValidationCode, ValidationIssue
from zeroth.contracts.mappings import MappingValidator


def _agent(node_id: str, bindings: list[AgentToolBinding] | None = None) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        agent=AgentNodeData(
            instruction="go",
            model_provider="p",
            tool_bindings=bindings or [],
        ),
    )


def _unit(node_id: str) -> ExecutableUnitNode:
    return ExecutableUnitNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="eu://x",
            execution_mode="wrapped_command",
        ),
    )


def _graph(nodes: list[Node], edges: list[Edge]) -> Graph:
    return Graph(
        graph_id="g",
        name="G",
        entry_step=nodes[0].node_id,
        nodes=nodes,
        edges=edges,
    )


def _run_edges(graph: Graph, node_map: dict[str, Node] | None = None) -> tuple[
    list[ValidationIssue],
    dict[str, list[str]],
]:
    resolved = {node.node_id: node for node in graph.nodes} if node_map is None else node_map
    # The caller owns the adjacency map and supplies a defaultdict, exactly as
    # GraphValidator does -- edge validation appends to it without seeding keys.
    adjacency: dict[str, list[str]] = defaultdict(list)
    issues: list[ValidationIssue] = []
    validate_edges(
        graph,
        resolved,
        set(),
        adjacency,
        issues,
        mapping_validator=MappingValidator(),
    )
    return issues, adjacency


def test_duplicate_edge_ids_are_reported() -> None:
    graph = _graph(
        [_agent("a"), _agent("b")],
        [
            Edge(edge_id="e1", source_node_id="a", target_node_id="b"),
            Edge(edge_id="e1", source_node_id="b", target_node_id="a"),
        ],
    )
    issues, _ = _run_edges(graph)

    assert [issue.code for issue in issues] == [ValidationCode.DUPLICATE_EDGE_ID]


def test_unknown_endpoints_are_reported() -> None:
    graph = _graph(
        [_agent("a"), _agent("b")],
        [Edge(edge_id="e1", source_node_id="a", target_node_id="b")],
    )
    # An empty node_map stands in for nodes that failed their own validation.
    issues, _ = _run_edges(graph, node_map={})

    assert [issue.code for issue in issues] == [
        ValidationCode.UNKNOWN_EDGE_SOURCE,
        ValidationCode.UNKNOWN_EDGE_TARGET,
    ]


def test_data_edges_build_the_control_flow_adjacency() -> None:
    graph = _graph(
        [_agent("a"), _unit("u")],
        [
            Edge(edge_id="e1", source_node_id="a", target_node_id="u"),
            Edge(edge_id="t1", source_node_id="a", target_node_id="u", kind="tool"),
        ],
    )
    _, adjacency = _run_edges(graph)

    # Tool edges attach tools; they are never traversed as control flow.
    assert dict(adjacency) == {"a": ["u"]}


def test_tool_edge_endpoints_and_payload_are_checked() -> None:
    graph = _graph(
        [_agent("a"), _unit("u")],
        [
            Edge(
                edge_id="t1",
                source_node_id="u",
                target_node_id="a",
                kind="tool",
                condition=Condition(expression="x"),
            )
        ],
    )
    issues, _ = _run_edges(graph)

    assert [issue.message for issue in issues] == [
        "tool edge source must be an agent node",
        "tool edge target must be an executable unit or code node",
        "tool edges cannot carry conditions or mappings",
        # The condition itself is still validated as a payload.
    ]


def _run_tools(graph: Graph) -> list[ValidationIssue]:
    node_map: dict[str, Node] = {node.node_id: node for node in graph.nodes}
    issues: list[ValidationIssue] = []
    validate_tool_attachments(graph, node_map, issues, capability_checks=NullCapabilityChecks())
    return issues


def test_attached_tool_without_a_binding_is_reported() -> None:
    graph = _graph(
        [_agent("a"), _unit("u")],
        [Edge(edge_id="t1", source_node_id="a", target_node_id="u", kind="tool")],
    )
    (issue,) = _run_tools(graph)

    assert issue.code is ValidationCode.INVALID_TOOL_BINDING
    assert "needs a binding" in issue.message


def test_binding_pointing_at_an_unattached_unit_is_reported() -> None:
    graph = _graph(
        [_agent("a", [AgentToolBinding(target_node_id="u", name="t", description="d")]), _unit("u")],
        [],
    )
    (issue,) = _run_tools(graph)

    assert "is not attached by a tool edge" in issue.message


def test_duplicate_tool_names_are_reported() -> None:
    bindings = [
        AgentToolBinding(target_node_id="u", name="same", description="one"),
        AgentToolBinding(target_node_id="u", name="same", description="two"),
    ]
    graph = _graph(
        [_agent("a", bindings), _unit("u")],
        [Edge(edge_id="t1", source_node_id="a", target_node_id="u", kind="tool")],
    )
    messages = [issue.message for issue in _run_tools(graph)]

    assert messages == [
        "attached tool 'u' has multiple bindings",
        "tool names must be unique per agent: same",
    ]


def test_capability_grants_are_delegated_once_per_agent() -> None:
    seen: list[str] = []

    class Recording(NullCapabilityChecks):
        def validate_tool_grants(
            self,
            graph: Graph,
            node: AgentNode,
            node_map: dict[str, Node],
            issues: list[ValidationIssue],
        ) -> None:
            seen.append(node.node_id)

    graph = _graph([_agent("a"), _agent("b"), _unit("u")], [])
    node_map: dict[str, Node] = {node.node_id: node for node in graph.nodes}
    validate_tool_attachments(graph, node_map, [], capability_checks=Recording())

    assert seen == ["a", "b"]

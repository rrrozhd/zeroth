"""Edge-payload validation (conditions, mappings) and cycle detection.

Both are pure contract concerns: they read the graph and nothing else. They
are extracted before ``edges`` because edge validation calls into them.
"""

from __future__ import annotations

from zeroth.contracts.graph.validation.cycles import (
    strongly_connected_components,
    validate_cycles,
)
from zeroth.contracts.graph.validation.mappings import (
    validate_condition,
    validate_mapping,
)
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
    Node,
)
from zeroth.contracts.graph.validation_errors import ValidationCode, ValidationIssue
from zeroth.contracts.mappings import MappingValidator
from zeroth.contracts.mappings.models import EdgeMapping, PassthroughMappingOperation


def _agent(node_id: str) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        agent=AgentNodeData(instruction="go", model_provider="p"),
    )


def _cycle_graph(*, safeguard: bool) -> Graph:
    return Graph(
        graph_id="g",
        name="G",
        entry_step="a",
        execution_settings=ExecutionSettings(max_visits_per_edge=2 if safeguard else None),
        nodes=[_agent("a"), _agent("b")],
        edges=[
            Edge(edge_id="e1", source_node_id="a", target_node_id="b"),
            Edge(edge_id="e2", source_node_id="b", target_node_id="a"),
        ],
    )


def _run_cycles(graph: Graph) -> list[ValidationIssue]:
    node_map: dict[str, Node] = {node.node_id: node for node in graph.nodes}
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source_node_id, []).append(edge.target_node_id)
    issues: list[ValidationIssue] = []
    validate_cycles(graph, node_map, adjacency, issues)
    return issues


def test_empty_condition_expression_is_reported() -> None:
    edge = Edge(
        edge_id="e1",
        source_node_id="a",
        target_node_id="b",
        condition=Condition(expression="   ", operand_refs=["ok", "  "]),
    )
    issues: list[ValidationIssue] = []
    validate_condition("g", edge, issues)

    assert [(issue.code, issue.path) for issue in issues] == [
        (ValidationCode.INVALID_CONDITION, ("edges", "e1", "condition", "expression")),
        (ValidationCode.INVALID_CONDITION, ("edges", "e1", "condition", "operand_refs", "1")),
    ]


def test_valid_condition_produces_nothing() -> None:
    edge = Edge(
        edge_id="e1",
        source_node_id="a",
        target_node_id="b",
        condition=Condition(expression="payload.ok == true", operand_refs=["payload.ok"]),
    )
    issues: list[ValidationIssue] = []
    validate_condition("g", edge, issues)

    assert issues == []


def test_invalid_mapping_surfaces_the_validator_message() -> None:
    edge = Edge(
        edge_id="e1",
        source_node_id="a",
        target_node_id="b",
        mapping=EdgeMapping(
            operations=[
                PassthroughMappingOperation(source_path="a.b", target_path="x.y"),
                PassthroughMappingOperation(source_path="c.d", target_path="x.y"),
            ]
        ),
    )
    issues: list[ValidationIssue] = []
    validate_mapping("g", edge, issues, mapping_validator=MappingValidator())

    (issue,) = issues
    assert issue.code is ValidationCode.INVALID_MAPPING
    assert issue.path == ("edges", "e1", "mapping")
    assert issue.details == {"error": issue.message}


def test_valid_mapping_produces_nothing() -> None:
    edge = Edge(
        edge_id="e1",
        source_node_id="a",
        target_node_id="b",
        mapping=EdgeMapping(
            operations=[PassthroughMappingOperation(source_path="a.b", target_path="x.y")]
        ),
    )
    issues: list[ValidationIssue] = []
    validate_mapping("g", edge, issues, mapping_validator=MappingValidator())

    assert issues == []


def test_cycle_without_safeguard_is_reported() -> None:
    (issue,) = _run_cycles(_cycle_graph(safeguard=False))

    assert issue.code is ValidationCode.UNSAFE_CYCLE
    assert issue.message == "cyclic graph path must declare a safeguard"
    assert issue.details == {"nodes": ["a", "b"], "edges": ["e1", "e2"]}


def test_cycle_with_a_visit_cap_is_allowed() -> None:
    assert _run_cycles(_cycle_graph(safeguard=True)) == []


def test_cycle_with_an_opted_in_condition_is_allowed() -> None:
    graph = _cycle_graph(safeguard=False)
    graph = graph.model_copy(
        update={
            "edges": [
                graph.edges[0],
                graph.edges[1].model_copy(
                    update={"condition": Condition(expression="x", allow_cycle_traversal=True)}
                ),
            ]
        }
    )
    assert _run_cycles(graph) == []


def test_acyclic_graph_is_clean() -> None:
    graph = Graph(
        graph_id="g",
        name="G",
        entry_step="a",
        execution_settings=ExecutionSettings(max_visits_per_edge=None),
        nodes=[_agent("a"), _agent("b")],
        edges=[Edge(edge_id="e1", source_node_id="a", target_node_id="b")],
    )
    assert _run_cycles(graph) == []


def test_strongly_connected_components_groups_mutually_reachable_nodes() -> None:
    adjacency = {"a": ["b"], "b": ["a"], "c": ["d"], "d": []}
    components = strongly_connected_components(["a", "b", "c", "d"], adjacency)

    assert {frozenset(component) for component in components} == {
        frozenset({"a", "b"}),
        frozenset({"c"}),
        frozenset({"d"}),
    }

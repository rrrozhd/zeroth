"""Edge wiring and tool-edge structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.contracts.graph.models import AgentNode, ExecutableUnitNode
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.mappings import validate_condition, validate_mapping
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.contracts.graph.models import Edge, Graph, Node
    from zeroth.contracts.mappings import MappingValidator


def validate_edges(
    graph: Graph,
    node_map: dict[str, Node],
    edge_ids: set[str],
    adjacency: dict[str, list[str]],
    issues: list[ValidationIssue],
    *,
    mapping_validator: MappingValidator,
) -> None:
    """Validate edge wiring and edge-level payloads.

    This checks for duplicate IDs, unknown source or target nodes, and
    invalid condition or mapping payloads.
    """
    for edge in graph.edges:
        if edge.edge_id in edge_ids:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.DUPLICATE_EDGE_ID,
                message=f"duplicate edge id: {edge.edge_id}",
                graph_id=graph.graph_id,
                edge_id=edge.edge_id,
            )
        edge_ids.add(edge.edge_id)

        if edge.source_node_id not in node_map:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.UNKNOWN_EDGE_SOURCE,
                message=f"edge source does not exist: {edge.source_node_id}",
                graph_id=graph.graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "source_node_id"),
                details={"source_node_id": edge.source_node_id},
            )
        elif edge.kind != "tool":
            # Tool edges attach tools rather than route execution, so they
            # stay out of the control-flow adjacency (and cycle checks).
            adjacency[edge.source_node_id].append(edge.target_node_id)

        if edge.target_node_id not in node_map:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.UNKNOWN_EDGE_TARGET,
                message=f"edge target does not exist: {edge.target_node_id}",
                graph_id=graph.graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "target_node_id"),
                details={"target_node_id": edge.target_node_id},
            )

        if edge.kind == "tool":
            validate_tool_edge(graph.graph_id, edge, node_map, issues)

        if edge.condition is not None:
            validate_condition(graph.graph_id, edge, issues)

        if edge.mapping is not None:
            validate_mapping(
                graph.graph_id,
                edge,
                issues,
                mapping_validator=mapping_validator,
            )


def validate_tool_edge(
    graph_id: str,
    edge: Edge,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
) -> None:
    """Check a tool edge's endpoints: agent source, executable-unit target.

    Conditions and mappings belong to control flow; a tool edge carrying
    either is a sign the author meant a data edge.
    """
    source = node_map.get(edge.source_node_id)
    if source is not None and not isinstance(source, AgentNode):
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_TOOL_EDGE,
            message="tool edge source must be an agent node",
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id, "source_node_id"),
            details={"source_node_id": edge.source_node_id},
        )
    target = node_map.get(edge.target_node_id)
    if target is not None and not isinstance(target, ExecutableUnitNode):
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_TOOL_EDGE,
            message="tool edge target must be an executable unit or code node",
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id, "target_node_id"),
            details={"target_node_id": edge.target_node_id},
        )
    if edge.condition is not None or edge.mapping is not None:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_TOOL_EDGE,
            message="tool edges cannot carry conditions or mappings",
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id),
        )

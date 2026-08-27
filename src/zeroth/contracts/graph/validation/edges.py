"""Edge wiring and tool-edge structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.contracts.graph.models import AgentNode, ExecutableUnitNode, MCPToolNode
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
        else:
            validate_data_edge(graph.graph_id, edge, node_map, issues)

        if edge.condition is not None:
            validate_condition(graph.graph_id, edge, issues)

        if edge.mapping is not None:
            validate_mapping(
                graph.graph_id,
                edge,
                issues,
                mapping_validator=mapping_validator,
            )


def validate_data_edge(
    graph_id: str,
    edge: Edge,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
) -> None:
    """Keep ``mcp_tool`` nodes out of control flow.

    An ``mcp_tool`` node is a tool *target*: it is reached through a tool edge
    from an agent and executed by that agent's tool bridge, never by being
    routed to. The node dispatcher has no branch for it, so a data edge into or
    out of one published with an error set identical to ``edges: []`` and then
    failed the run with ``unsupported node type``. That is the same divergence
    between publish and dispatch the capability floor exists to close, and the
    feature's premise is that these failures surface at publish.

    The canvas only offers the node a tool-target port, so this is unreachable
    from the editor -- but the API and a hand-written JSON graph are wider than
    the canvas, which is why the rule belongs to validation rather than to the
    editor. An endpoint that does not resolve is left alone: it is already
    reported as an unknown source or target, and ``node_map.get`` returning
    ``None`` is not an ``MCPToolNode``, so no edge is named twice.

    A disabled edge is left alone too. The claim being made here is about
    *dispatch reachability*, and every traversal on the execution path filters
    ``edge.enabled`` (``token_scope.py``, ``driver.py``, ``Graph`` itself), so a
    disabled edge never reaches the node dispatcher and cannot raise
    ``unsupported node type``. Rejecting one would block publish on an edge the
    author has already switched off, clearable only by deleting it. This
    follows ``validate_tool_attachments``, which honours the flag, rather than
    the adjacency build above, which does not.
    """
    if not edge.enabled:
        return
    for role, node_id in (
        ("source", edge.source_node_id),
        ("target", edge.target_node_id),
    ):
        if not isinstance(node_map.get(node_id), MCPToolNode):
            continue
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message=(
                f"data edge {role} {node_id!r} is an mcp_tool node; an mcp_tool node is "
                "reached only through a tool edge from the agent that binds it, so a "
                "control-flow edge would fail the run at dispatch"
            ),
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id, f"{role}_node_id"),
            details={"node_id": node_id, "edge_role": role},
        )


def validate_tool_edge(
    graph_id: str,
    edge: Edge,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
) -> None:
    """Check a tool edge's endpoints: agent source, executable-unit or MCP-tool target.

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
    if target is not None and not isinstance(target, ExecutableUnitNode | MCPToolNode):
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_TOOL_EDGE,
            message="tool edge target must be an executable unit, code, or MCP tool node",
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

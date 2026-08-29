"""Topology invariants for explicit control-flow nodes."""

from __future__ import annotations

import re
from collections import Counter

from zeroth.contracts.graph.models import Condition, Edge, Graph, IfNode, Node
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

LEGACY_IF_ROUTES = frozenset({"true", "false"})


def canonical_if_route_condition(node_id: str, route: str) -> Condition:
    """Return the hidden runtime predicate for one named If output."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", route):
        raise ValueError(f"invalid If route id: {route!r}")
    safe_node_id = node_id.replace("\\", "\\\\").replace("'", "\\'")
    safe_route = route.replace("\\", "\\\\").replace("'", "\\'")
    return Condition(
        expression=f"payload.zeroth_if['{safe_node_id}'].route == '{safe_route}'",
        branch_rule="expression",
        operand_refs=[],
        allow_cycle_traversal=False,
        metadata={"if_route": route},
    )


def canonicalize_if_route_edges(nodes: list[Node], edges: list[Edge]) -> list[Edge]:
    """Server-normalize Studio If routes rather than trusting browser predicates."""
    if_routes = {
        node.node_id: (
            {route.route_id for route in node.condition.routes}
            if node.condition.routes
            else set(LEGACY_IF_ROUTES)
        )
        for node in nodes
        if isinstance(node, IfNode)
    }
    normalized: list[Edge] = []
    for edge in edges:
        route = edge.metadata.get("source_handle")
        if (
            edge.source_node_id in if_routes
            and edge.kind != "tool"
            and route in if_routes[edge.source_node_id]
        ):
            edge = edge.model_copy(
                update={"condition": canonical_if_route_condition(edge.source_node_id, route)}
            )
        normalized.append(edge)
    return normalized


def validate_control_nodes(
    graph: Graph,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
) -> None:
    """Require every If route to be explicit, canonical, and non-duplicated."""
    for node in graph.nodes:
        if not isinstance(node, IfNode):
            continue
        outgoing = [edge for edge in graph.edges if edge.source_node_id == node.node_id]
        active_data = [edge for edge in outgoing if edge.enabled and edge.kind != "tool"]
        allowed_routes = (
            {route.route_id for route in node.condition.routes}
            if node.condition.routes
            else set(LEGACY_IF_ROUTES)
        )
        if not active_data:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_CONDITION,
                message="If node must connect at least one configured route",
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "condition"),
            )
            continue

        routes: list[str] = []
        for edge in active_data:
            route = edge.metadata.get("source_handle")
            if route not in allowed_routes:
                _append_route_issue(
                    graph, node, edge, issues, "If edges must use a configured output route"
                )
                continue
            routes.append(route)
            expected = canonical_if_route_condition(node.node_id, route)
            if edge.condition != expected:
                _append_route_issue(
                    graph,
                    node,
                    edge,
                    issues,
                    f"If {route.title()} route condition is missing or non-canonical",
                )

        for route, count in Counter(routes).items():
            if count > 1:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_CONDITION,
                    message=f"If {route.title()} output may connect to only one target",
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "condition"),
                    details={"route": route, "count": count},
                )


def _append_route_issue(
    graph: Graph,
    node: IfNode,
    edge: Edge,
    issues: list[ValidationIssue],
    message: str,
) -> None:
    append_issue(
        issues,
        severity=ValidationSeverity.ERROR,
        code=ValidationCode.INVALID_CONDITION,
        message=message,
        graph_id=graph.graph_id,
        node_id=node.node_id,
        edge_id=edge.edge_id,
        path=("edges", edge.edge_id, "condition"),
    )

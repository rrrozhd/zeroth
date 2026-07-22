"""Cross-checking tool edges against each agent's declared tool bindings."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from zeroth.contracts.graph.models import AgentNode
from zeroth.contracts.graph.validation.capabilities import CapabilityChecks
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.contracts.graph.models import Graph, Node


def validate_tool_attachments(
    graph: Graph,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
    *,
    capability_checks: CapabilityChecks,
) -> None:
    """Cross-check tool edges against each agent's tool bindings.

    Every attached unit needs exactly one author-provided binding (name,
    description, argument descriptions — enforced by the binding model),
    names must be unique per agent, and bindings must not point at units
    that are no longer attached.
    """
    tool_targets: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "tool" and edge.enabled:
            tool_targets[edge.source_node_id].append(edge.target_node_id)

    for node in graph.nodes:
        if not isinstance(node, AgentNode):
            continue
        attached = tool_targets.get(node.node_id, [])
        bound_targets = [binding.target_node_id for binding in node.agent.tool_bindings]

        for target_id in attached:
            if bound_targets.count(target_id) == 0:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_TOOL_BINDING,
                    message=(
                        f"attached tool {target_id!r} needs a binding with a "
                        "name, description, and argument descriptions"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "tool_bindings"),
                    details={"target_node_id": target_id},
                )
            elif bound_targets.count(target_id) > 1:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_TOOL_BINDING,
                    message=f"attached tool {target_id!r} has multiple bindings",
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "tool_bindings"),
                    details={"target_node_id": target_id},
                )

        for binding in node.agent.tool_bindings:
            if binding.target_node_id not in attached:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_TOOL_BINDING,
                    message=(
                        f"tool binding {binding.name!r} points at "
                        f"{binding.target_node_id!r}, which is not attached by a tool edge"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "tool_bindings"),
                    details={"target_node_id": binding.target_node_id},
                )

        names = [binding.name for binding in node.agent.tool_bindings]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_TOOL_BINDING,
                message=f"tool names must be unique per agent: {', '.join(duplicates)}",
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "agent", "tool_bindings"),
                details={"duplicate_names": duplicates},
            )

        capability_checks.validate_tool_grants(graph, node, node_map, issues)

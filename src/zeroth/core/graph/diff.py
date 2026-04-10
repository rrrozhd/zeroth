"""Compare two graph versions and list what changed between them.

This module is like a "git diff" for graphs.  Given two versions of a graph,
it produces a structured report of added, removed, and modified nodes, edges,
conditions, contracts, policies, memory connectors, and executable bindings.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.graph.models import AgentNode, Edge, ExecutableUnitNode, Graph, HumanApprovalNode, Node

ChangeType = Literal["added", "removed", "modified"]


class DiffEntry(BaseModel):
    """A single change found between two graph versions.

    Records what entity changed, what kind of change it was (added, removed,
    or modified), and the before/after values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    change_type: ChangeType
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changed_fields: list[str] = Field(default_factory=list)


class GraphDiff(BaseModel):
    """The full comparison result between two graph versions.

    Contains lists of changes grouped by category: nodes, edges, conditions,
    contracts, policies, memory connectors, and executable unit bindings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    left_graph_id: str
    left_version: int
    right_graph_id: str
    right_version: int
    node_changes: list[DiffEntry] = Field(default_factory=list)
    edge_changes: list[DiffEntry] = Field(default_factory=list)
    condition_changes: list[DiffEntry] = Field(default_factory=list)
    contract_changes: list[DiffEntry] = Field(default_factory=list)
    policy_changes: list[DiffEntry] = Field(default_factory=list)
    memory_connector_changes: list[DiffEntry] = Field(default_factory=list)
    executable_unit_binding_changes: list[DiffEntry] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """Return True if there are no changes at all between the two versions."""
        return all(
            not bucket
            for bucket in (
                self.node_changes,
                self.edge_changes,
                self.condition_changes,
                self.contract_changes,
                self.policy_changes,
                self.memory_connector_changes,
                self.executable_unit_binding_changes,
            )
        )


def diff_graphs(left: Graph, right: Graph) -> GraphDiff:
    """Compare two graph versions and classify semantic changes."""
    (
        node_changes,
        contract_changes,
        policy_changes,
        memory_changes,
        executable_changes,
    ) = _diff_nodes(
        left.nodes,
        right.nodes,
    )
    edge_changes, condition_changes = _diff_edges(left.edges, right.edges)

    if left.policy_bindings != right.policy_bindings:
        policy_changes.append(
            DiffEntry(
                entity_id=left.graph_id,
                change_type="modified",
                before={"policy_bindings": list(left.policy_bindings)},
                after={"policy_bindings": list(right.policy_bindings)},
                changed_fields=["policy_bindings"],
            )
        )

    return GraphDiff(
        left_graph_id=left.graph_id,
        left_version=left.version,
        right_graph_id=right.graph_id,
        right_version=right.version,
        node_changes=node_changes,
        edge_changes=edge_changes,
        condition_changes=condition_changes,
        contract_changes=contract_changes,
        policy_changes=policy_changes,
        memory_connector_changes=memory_changes,
        executable_unit_binding_changes=executable_changes,
    )


def _diff_nodes(
    left_nodes: Iterable[Node],
    right_nodes: Iterable[Node],
) -> tuple[list[DiffEntry], list[DiffEntry], list[DiffEntry], list[DiffEntry], list[DiffEntry]]:
    """Compare two sets of nodes and return changes for nodes,
    contracts, policies, memory, and executables."""
    left_map = {node.node_id: node for node in left_nodes}
    right_map = {node.node_id: node for node in right_nodes}

    node_changes: list[DiffEntry] = []
    contract_changes: list[DiffEntry] = []
    policy_changes: list[DiffEntry] = []
    memory_changes: list[DiffEntry] = []
    executable_changes: list[DiffEntry] = []

    # Union of all node IDs from both versions so we catch adds, removes, and changes
    for node_id in sorted(left_map.keys() | right_map.keys()):
        left_node = left_map.get(node_id)
        right_node = right_map.get(node_id)

        if left_node is None and right_node is not None:
            node_changes.append(_entry(node_id, "added", None, right_node))
            _collect_semantic_changes(
                node_id,
                None,
                right_node,
                contract_changes,
                policy_changes,
                memory_changes,
                executable_changes,
            )
            continue

        if right_node is None and left_node is not None:
            node_changes.append(_entry(node_id, "removed", left_node, None))
            _collect_semantic_changes(
                node_id,
                left_node,
                None,
                contract_changes,
                policy_changes,
                memory_changes,
                executable_changes,
            )
            continue

        if left_node is None or right_node is None:
            continue

        left_dump = _node_dump(left_node)
        right_dump = _node_dump(right_node)
        changed_fields = _changed_fields(left_dump, right_dump)
        if changed_fields:
            node_changes.append(_entry(node_id, "modified", left_node, right_node, changed_fields))
        _collect_semantic_changes(
            node_id,
            left_node,
            right_node,
            contract_changes,
            policy_changes,
            memory_changes,
            executable_changes,
        )

    return node_changes, contract_changes, policy_changes, memory_changes, executable_changes


def _collect_semantic_changes(
    node_id: str,
    left_node: Node | None,
    right_node: Node | None,
    contract_changes: list[DiffEntry],
    policy_changes: list[DiffEntry],
    memory_changes: list[DiffEntry],
    executable_changes: list[DiffEntry],
) -> None:
    """Check a single node pair for contract, policy, memory, and executable changes."""
    left_dump = _node_dump(left_node) if left_node is not None else None
    right_dump = _node_dump(right_node) if right_node is not None else None

    left_contracts = _select_fields(left_dump, ["input_contract_ref", "output_contract_ref"])
    right_contracts = _select_fields(right_dump, ["input_contract_ref", "output_contract_ref"])
    if left_contracts != right_contracts:
        contract_changes.append(
            DiffEntry(
                entity_id=node_id,
                change_type=_change_type(left_dump, right_dump),
                before=left_contracts,
                after=right_contracts,
                changed_fields=_changed_field_names(left_contracts, right_contracts),
            )
        )

    left_policies = _select_fields(left_dump, ["policy_bindings", "capability_bindings"])
    right_policies = _select_fields(right_dump, ["policy_bindings", "capability_bindings"])
    if left_policies != right_policies:
        policy_changes.append(
            DiffEntry(
                entity_id=node_id,
                change_type=_change_type(left_dump, right_dump),
                before=left_policies,
                after=right_policies,
                changed_fields=_changed_field_names(left_policies, right_policies),
            )
        )

    left_memory = _memory_refs(left_node)
    right_memory = _memory_refs(right_node)
    if left_memory != right_memory:
        memory_changes.append(
            DiffEntry(
                entity_id=node_id,
                change_type=_change_type(left_dump, right_dump),
                before={"memory_refs": left_memory} if left_memory is not None else None,
                after={"memory_refs": right_memory} if right_memory is not None else None,
                changed_fields=["memory_refs"],
            )
        )

    left_exec = _executable_binding(left_node)
    right_exec = _executable_binding(right_node)
    if left_exec != right_exec:
        executable_changes.append(
            DiffEntry(
                entity_id=node_id,
                change_type=_change_type(left_dump, right_dump),
                before=left_exec,
                after=right_exec,
                changed_fields=_changed_field_names(left_exec, right_exec),
            )
        )


def _diff_edges(
    left_edges: Iterable[Edge],
    right_edges: Iterable[Edge],
) -> tuple[list[DiffEntry], list[DiffEntry]]:
    """Compare two sets of edges and return edge changes and condition changes."""
    left_map = {edge.edge_id: edge for edge in left_edges}
    right_map = {edge.edge_id: edge for edge in right_edges}
    edge_changes: list[DiffEntry] = []
    condition_changes: list[DiffEntry] = []

    for edge_id in sorted(left_map.keys() | right_map.keys()):
        left_edge = left_map.get(edge_id)
        right_edge = right_map.get(edge_id)

        if left_edge is None and right_edge is not None:
            edge_changes.append(_entry(edge_id, "added", None, right_edge))
            if right_edge.condition is not None:
                condition_changes.append(_condition_entry(edge_id, None, right_edge.condition))
            continue

        if right_edge is None and left_edge is not None:
            edge_changes.append(_entry(edge_id, "removed", left_edge, None))
            if left_edge.condition is not None:
                condition_changes.append(_condition_entry(edge_id, left_edge.condition, None))
            continue

        if left_edge is None or right_edge is None:
            continue

        left_dump = _edge_dump(left_edge)
        right_dump = _edge_dump(right_edge)
        changed_fields = _changed_fields(left_dump, right_dump)
        if changed_fields:
            edge_changes.append(_entry(edge_id, "modified", left_edge, right_edge, changed_fields))

        if _condition_dump(left_edge) != _condition_dump(right_edge):
            condition_changes.append(
                _condition_entry(edge_id, left_edge.condition, right_edge.condition)
            )

    return edge_changes, condition_changes


def _entry(
    entity_id: str,
    change_type: ChangeType,
    before: object | None,
    after: object | None,
    changed_fields: list[str] | None = None,
) -> DiffEntry:
    """Build a DiffEntry from raw before/after objects."""
    return DiffEntry(
        entity_id=entity_id,
        change_type=change_type,
        before=_dump(before),
        after=_dump(after),
        changed_fields=changed_fields or [],
    )


def _condition_entry(
    edge_id: str,
    before: object | None,
    after: object | None,
) -> DiffEntry:
    """Build a DiffEntry specifically for a condition change on an edge."""
    return DiffEntry(
        entity_id=edge_id,
        change_type=_change_type(_dump(before), _dump(after)),
        before=_dump(before),
        after=_dump(after),
        changed_fields=_changed_field_names(_dump(before), _dump(after)),
    )


def _dump(value: object | None) -> dict[str, Any] | None:
    """Convert a model or dict to a plain dictionary, or return None."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    if isinstance(value, dict):
        return value
    return {"value": value}


def _node_dump(node: Node | None) -> dict[str, Any]:
    """Dump a node to a dict, excluding the graph version ref for comparison."""
    if node is None:
        return {}
    return node.model_dump(mode="json", exclude={"graph_version_ref"})


def _edge_dump(edge: Edge) -> dict[str, Any]:
    """Dump an edge to a dict, excluding mapping and condition for comparison."""
    return edge.model_dump(mode="json", exclude={"mapping", "condition"})


def _condition_dump(edge: Edge) -> dict[str, Any] | None:
    """Dump just the condition part of an edge, or None if there is no condition."""
    if edge.condition is None:
        return None
    return edge.condition.model_dump(mode="json")


def _select_fields(value: dict[str, Any], fields: list[str]) -> dict[str, Any] | None:
    """Pick only the named fields from a dict. Returns None if none are present."""
    selected = {field: value[field] for field in fields if field in value}
    return selected or None


def _changed_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Return the names of fields that differ between two dicts."""
    return _changed_field_names(left, right)


def _changed_field_names(left: dict[str, Any] | None, right: dict[str, Any] | None) -> list[str]:
    """Return sorted field names whose values differ, handling None dicts."""
    left = left or {}
    right = right or {}
    return [
        field for field in sorted(left.keys() | right.keys()) if left.get(field) != right.get(field)
    ]


def _change_type(left: dict[str, Any] | None, right: dict[str, Any] | None) -> ChangeType:
    """Determine if something was added, removed, or modified based on None-ness."""
    if left is None and right is not None:
        return "added"
    if right is None and left is not None:
        return "removed"
    return "modified"


def _memory_refs(node: Node | None) -> list[str] | None:
    """Extract memory references from an agent node, or None for other types."""
    if isinstance(node, AgentNode):
        return list(node.agent.memory_refs)
    if node is None:
        return None
    return None


def _executable_binding(node: Node | None) -> dict[str, Any] | None:
    """Extract executable unit binding details from a node, or None for other types."""
    if isinstance(node, ExecutableUnitNode):
        return {
            "manifest_ref": node.executable_unit.manifest_ref,
            "execution_mode": node.executable_unit.execution_mode,
            "runtime_binding": node.executable_unit.runtime_binding,
            "sandbox_config": node.executable_unit.sandbox_config,
            "output_extraction_strategy": node.executable_unit.output_extraction_strategy,
        }
    if isinstance(node, HumanApprovalNode):
        return None
    if node is None:
        return None
    return None

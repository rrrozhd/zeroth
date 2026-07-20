"""B9 sequential-join publish checks: JoinConfig presence and cycle exclusion."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from zeroth.contracts.graph.validation.cycles import strongly_connected_components
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.contracts.graph.models import Edge, Graph, Node


def validate_join_configs(
    graph: Graph,
    node_map: dict[str, Node],
    adjacency: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> None:
    """B9: require a ``JoinConfig`` on genuinely-convergent nodes (flag-gated).

    This is the ONLY new validation added by the sequential join barrier, and
    it fires solely when ``execution_settings.sequential_join_enabled`` is set
    — so with the flag off (default) publish validation is byte-identical to
    pre-B9 and no shipping graph is newly rejected.

    Two rules, both narrow:

    * **MISSING_JOIN_CONFIG** — a node with >=2 *unconditional* non-tool
      inbound edges (``condition is None``) has genuine concurrent delivery
      and must declare how the payloads merge. Conditional reconvergence
      (mutually-exclusive inbound, e.g. the vendor-dd ``report`` node) has at
      most one unconditional inbound and is never flagged.
    * **JOIN_ON_CYCLE** — a convergent node (>=2 non-tool inbound edges) that
      sits inside a cycle. Per-iteration loop re-join (design §4.4) is
      deferred; such a node would wait forever for a back-edge that only
      delivers on a later iteration, silently mis-completing the run. Reject
      loudly rather than ship the wrong behaviour behind the flag.

    ``parallel_config`` nodes own their own fan-in and are never subject to
    the sequential join barrier, so they are skipped entirely.
    """
    if not graph.execution_settings.sequential_join_enabled:
        return

    inbound_by_target: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "tool" or not edge.enabled:
            continue
        if edge.source_node_id in node_map and edge.target_node_id in node_map:
            inbound_by_target[edge.target_node_id].append(edge)

    cyclic_nodes: set[str] = set()
    for component in strongly_connected_components(node_map.keys(), adjacency):
        if len(component) > 1:
            cyclic_nodes |= component
        else:
            node_id = next(iter(component))
            if node_id in adjacency.get(node_id, []):
                cyclic_nodes.add(node_id)

    for node_id, inbound_edges in inbound_by_target.items():
        node = node_map[node_id]
        if getattr(node, "parallel_config", None) is not None:
            continue
        if len(inbound_edges) < 2:
            continue
        if node_id in cyclic_nodes:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.JOIN_ON_CYCLE,
                message=(
                    f"convergent node {node_id!r} sits inside a cycle; "
                    "per-iteration join re-scoping is not yet supported under "
                    "sequential_join_enabled"
                ),
                graph_id=graph.graph_id,
                node_id=node_id,
                details={"inbound_edges": [e.edge_id for e in inbound_edges]},
            )
            continue
        unconditional = [e for e in inbound_edges if e.condition is None]
        if len(unconditional) >= 2 and getattr(node, "join_config", None) is None:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_JOIN_CONFIG,
                message=(
                    f"node {node_id!r} has {len(unconditional)} unconditional "
                    "inbound edges (concurrent delivery) but no join_config; "
                    "declare a JoinConfig merge policy"
                ),
                graph_id=graph.graph_id,
                node_id=node_id,
                details={"unconditional_inbound_edges": [e.edge_id for e in unconditional]},
            )

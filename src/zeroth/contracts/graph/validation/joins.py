"""B9 sequential-join publish checks: JoinConfig presence and role conflicts."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

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
    issues: list[ValidationIssue],
) -> None:
    """B9: require a ``JoinConfig`` on genuinely-convergent nodes (flag-gated).

    This is the ONLY new validation added by the sequential join barrier, and
    it fires solely when ``execution_settings.sequential_join_enabled`` is set
    — so with the flag off (default) publish validation is byte-identical to
    pre-B9 and no shipping graph is newly rejected.

    One rule:

    * **MISSING_JOIN_CONFIG** — a node with >=2 *unconditional* non-tool
      inbound edges (``condition is None``) has genuine concurrent delivery
      and must declare how the payloads merge. Conditional reconvergence
      (mutually-exclusive inbound, e.g. the vendor-dd ``report`` node) has at
      most one unconditional inbound and is never flagged.

    ``JOIN_ON_CYCLE`` used to reject a convergent node inside a cycle, because
    per-iteration re-join was deferred and such a node would have waited
    forever for a back-edge that only delivers on a later iteration. The
    runtime now scopes the join per iteration and re-enters a loop header
    through its back-edge, so a convergent node on a cycle runs correctly and
    the rule is gone.

    A ``parallel_config`` node is a fan-OUT; its INBOUND edges are still
    joined by the barrier before it runs, so a genuine multi-unconditional-
    inbound convergence must declare a JoinConfig even when it also fans out.
    """
    if not graph.execution_settings.sequential_join_enabled:
        return

    inbound_by_target: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "tool" or not edge.enabled:
            continue
        if edge.source_node_id in node_map and edge.target_node_id in node_map:
            inbound_by_target[edge.target_node_id].append(edge)

    # A node reached by an edge FROM a parallel_config node is a fan-out
    # successor: it runs once PER BRANCH. It therefore cannot ALSO be a join
    # target (audit re-review #4) — the two roles conflict, and the barrier
    # never records the fan-out edge for it, false-deadlocking the run. The
    # legitimate "fan out then combine" pattern puts the join ONE hop below
    # the fan-out successor, not on it.
    fanout_successors: set[str] = {
        edge.target_node_id
        for edge in graph.edges
        if edge.kind != "tool"
        and edge.enabled
        and edge.source_node_id in node_map
        and getattr(node_map[edge.source_node_id], "parallel_config", None) is not None
    }

    for node_id, inbound_edges in inbound_by_target.items():
        node = node_map[node_id]
        if len(inbound_edges) < 2:
            continue
        if node_id in fanout_successors:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.FANOUT_SUCCESSOR_JOIN,
                message=(
                    f"node {node_id!r} is the immediate successor of a "
                    "parallel fan-out (runs once per branch) and also has "
                    f"{len(inbound_edges)} inbound edges (a join target). These "
                    "roles conflict. Move the join one hop below the fan-out "
                    "successor so the fan-in result and the other input "
                    "converge at a separate node."
                ),
                graph_id=graph.graph_id,
                node_id=node_id,
                details={"inbound_edges": [e.edge_id for e in inbound_edges]},
            )
            continue
        # A parallel_config node is a fan-OUT; its INBOUND edges are still
        # joined by the barrier before it runs (B9 audit #6). So a genuine
        # multi-unconditional-inbound convergence must declare a JoinConfig
        # even when it also fans out — it is NOT exempt.
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

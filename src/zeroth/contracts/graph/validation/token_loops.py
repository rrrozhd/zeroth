"""B9 token-engine loop-model guards: reducibility, latches, fan-out-in-loop.

All checks are gated on ``execution_settings.sequential_join_enabled`` and run
over the ENABLED control-flow graph only — the same graph the token engine
executes (``token_scope`` drops disabled edges), so the publish-time loop model
cannot diverge from the runtime's.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
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


def dominators(
    entry: str,
    node_ids: Iterable[str],
    adjacency: dict[str, list[str]],
) -> dict[str, set[str]]:
    """Iterative dominator sets over the entry-reachable subgraph.

    ``d in dominators[n]`` iff every path from the entry to ``n`` passes
    through ``d``. Classic fixpoint: ``dom(entry) = {entry}``; for every other
    reachable node ``dom(n) = {n} | intersect(dom(p) for p in preds(n))``.
    Graph sizes here are small (workflow graphs), so the O(N * E) fixpoint is
    ample.
    """
    nodes = list(node_ids)
    preds: dict[str, list[str]] = {n: [] for n in nodes}
    for src, targets in adjacency.items():
        for dst in targets:
            if dst in preds:
                preds[dst].append(src)

    # Restrict to the entry-reachable set — unreachable nodes have no dominators.
    reachable: set[str] = set()
    stack = [entry]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(adjacency.get(node, []))

    all_nodes = set(reachable)
    dom: dict[str, set[str]] = {n: set(all_nodes) for n in reachable}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for node in reachable:
            if node == entry:
                continue
            reachable_preds = [p for p in preds[node] if p in reachable]
            new_dom = {node}
            if reachable_preds:
                inter = set(all_nodes)
                for p in reachable_preds:
                    inter &= dom[p]
                new_dom |= inter
            if new_dom != dom[node]:
                dom[node] = new_dom
                changed = True
    return dom


def natural_loop_bodies(
    graph: Graph,
    node_map: dict[str, Node],
    adjacency: dict[str, list[str]],
) -> dict[str, set[str]]:
    """Map each loop header to its natural-loop body (dominator back-edges).

    A back-edge is ``u -> v`` with ``v`` dominating ``u`` (well-defined and
    DFS-order-independent because the graph is reducible — enforced by
    :func:`validate_reducibility`). The natural loop of ``u -> v`` is ``v``
    plus every node that reaches ``u`` without passing through ``v``. Bodies of
    back-edges sharing a header are unioned. Computed over entry-reachable
    nodes only, so it agrees with the runtime's loop scoping on any graph that
    also passes the reducibility guard.
    """
    entry = graph.entry_step
    if entry is None or entry not in node_map:
        return {}
    dom = dominators(entry, node_map.keys(), adjacency)
    reachable = set(dom.keys())
    preds: dict[str, list[str]] = defaultdict(list)
    for src in reachable:
        for dst in adjacency.get(src, []):
            if dst in reachable:
                preds[dst].append(src)
    bodies: dict[str, set[str]] = defaultdict(set)
    for src in reachable:
        for dst in adjacency.get(src, []):
            if dst not in dom.get(src, set()):
                continue  # not a back-edge
            header, tail = dst, src
            body = {header}
            stack: list[str] = []
            if tail != header:
                body.add(tail)
                stack.append(tail)
            while stack:
                node = stack.pop()
                for pred in preds.get(node, []):
                    if pred not in body:
                        body.add(pred)
                        stack.append(pred)
            bodies[header] |= body
    return dict(bodies)


def validate_reducibility(
    graph: Graph,
    node_map: dict[str, Node],
    adjacency: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> None:
    """B9: reject IRREDUCIBLE control-flow loops (flag-gated).

    The sequential join barrier's loop handling rests on DFS back-edge
    classification and natural-loop bodies, which are only well-defined for
    **reducible** graphs. An irreducible loop — a cycle with two distinct
    entry points — makes "the back edge" DFS-order-dependent and a node's
    enclosing-loop set ambiguous, so the epoch model would bucket edges wrong
    and silently deadlock or double-dispatch. Reject loudly at publish rather
    than mis-execute behind the flag.

    Test (DFS-order-independent, because dominators are unique): compute
    dominators from the entry; a **retreating** edge ``u -> v`` is a genuine
    loop back-edge iff ``v`` dominates ``u``. Remove all such back-edges; if
    the remainder still contains a cycle, some cycle had no single dominating
    header — the graph is irreducible.
    """
    if not graph.execution_settings.sequential_join_enabled:
        return
    entry = graph.entry_step
    if entry is None or entry not in node_map:
        return

    dom = dominators(entry, node_map.keys(), adjacency)
    # Dominators are defined only over the entry-reachable subgraph, so the
    # whole reducibility check runs on reachable nodes only: an unreachable
    # component cannot execute, and including it would misclassify its
    # back-edges (no dominator entry) as a residual cycle → a false
    # IRREDUCIBLE_LOOP (audit re-review #7).
    reachable = set(dom.keys())
    # Back-edges: u -> v where v dominates u (v is a proper loop header for u).
    back_edges: set[tuple[str, str]] = set()
    for src, targets in adjacency.items():
        if src not in reachable:
            continue
        for dst in targets:
            if dst in dom.get(src, set()):
                back_edges.add((src, dst))
    # Do NOT early-return on an empty back-edge set: a cycle whose header does
    # not dominate its body has NO dominating back-edge, which is precisely the
    # irreducible case that must be caught below.

    # Remove back-edges; a cycle in the remainder means an irreducible loop.
    forward_adj: dict[str, list[str]] = {
        src: [dst for dst in targets if (src, dst) not in back_edges]
        for src, targets in adjacency.items()
        if src in reachable
    }
    residual_cycles = [
        component
        for component in strongly_connected_components(reachable, forward_adj)
        if len(component) > 1 or any(node in forward_adj.get(node, []) for node in component)
    ]
    if residual_cycles:
        offending = sorted({n for component in residual_cycles for n in component})
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.IRREDUCIBLE_LOOP,
            message=(
                "graph has an irreducible loop (a cycle with more than one "
                f"entry point) among nodes {offending}; the sequential join "
                "barrier requires reducible control flow. Restructure so the "
                "loop has a single entry (header)."
            ),
            graph_id=graph.graph_id,
            details={"nodes": offending},
        )
        return

    # Multi-latch: a header reached by >=2 back-edges (two latches). The
    # loop-epoch counter for such a header can advance from a latch that is
    # not downstream of a body join, splitting that join's inbound across
    # epochs (audit re-review #1). A single monotonic counter cannot key this
    # unambiguously; reject until a token-based join engine lands.
    latches_per_header: dict[str, list[str]] = defaultdict(list)
    for src, dst in back_edges:
        latches_per_header[dst].append(src)
    for header, latches in latches_per_header.items():
        if len(latches) >= 2:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MULTI_LATCH_LOOP,
                message=(
                    f"loop header {header!r} is reached by {len(latches)} "
                    "back-edges (multiple latches); the sequential join barrier "
                    "supports a single latch per loop header. Restructure the "
                    "loop to converge its back-edges through one node before "
                    "looping."
                ),
                graph_id=graph.graph_id,
                node_id=header,
                details={"latches": sorted(latches)},
            )


def validate_fanout_in_loop(
    graph: Graph,
    node_map: dict[str, Node],
    adjacency: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> None:
    """B9 token engine: reject fan-out INSIDE a loop (flag-gated).

    The token join engine circulates a single token per loop-iteration tag,
    so it cannot represent two concurrent tokens that share one iteration and
    do NOT reconverge. Two shapes create exactly that and are rejected:

    1. A ``parallel_config`` node inside a loop — its fan-out spawns multiple
       concurrent branches under one tag.
    2. A loop-body node that BOTH leaves the loop and continues it at the same
       time: it has an exit edge (target outside the loop body) AND a
       continuing edge (a back-edge or an in-loop forward edge), where at least
       one of them is UNCONDITIONAL (``condition is None``) and so is always
       active. The unconditional edge fires every iteration alongside the
       other, forking a token that leaves the loop while another keeps
       circulating — a non-reconverging multi-token. (In a single-latch
       reducible loop, in-loop fan-out otherwise always reconverges at the
       lone tail, which the engine DOES handle — e.g. a diamond inside a loop.)

    A clean loop exit — a decision node whose continue and exit edges are
    mutually-exclusive CONDITIONS (both non-``None``) — takes exactly one edge
    per visit and is a single token, so it is NOT rejected.
    """
    if not graph.execution_settings.sequential_join_enabled:
        return
    cyclic: set[str] = set()
    for component in strongly_connected_components(node_map.keys(), adjacency):
        if len(component) > 1:
            cyclic |= component
        else:
            node_id = next(iter(component))
            if node_id in adjacency.get(node_id, []):
                cyclic.add(node_id)
    for node_id in sorted(cyclic):
        if getattr(node_map[node_id], "parallel_config", None) is not None:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.FANOUT_IN_LOOP,
                message=(
                    f"parallel fan-out node {node_id!r} sits inside a loop; the "
                    "sequential join barrier does not yet support fan-out inside "
                    "a loop (concurrent tokens sharing a loop iteration). Move the "
                    "fan-out outside the loop, or gate it behind a subgraph."
                ),
                graph_id=graph.graph_id,
                node_id=node_id,
            )
    # Structural fan-out: a loop-body node that FORKS into >=2 concurrently
    # active tokens which do not all reconverge within one loop iteration.
    #
    # An edge is "concurrently active" if it always fires — an unconditional
    # forward edge — or if it is a back-edge (which fires whenever its loop
    # continues, so two back-edges, or a back-edge and an unconditional
    # forward, fire together). A node with >=2 such edges forks, UNLESS every
    # one is an unconditional forward staying inside the node's innermost loop:
    # in a single-latch reducible loop those all reconverge at the lone latch
    # (a diamond inside a loop), which the engine handles. Any other fork — an
    # edge leaving the loop, or a back-edge alongside another active edge —
    # splits the single circulating token and is rejected. Conditional edges
    # are assumed mutually exclusive (a clean decision), so a do-while's
    # ``step<K`` back-edge next to a ``step>=K`` exit is NOT a fork.
    bodies = natural_loop_bodies(graph, node_map, adjacency)
    if bodies:
        entry = graph.entry_step
        dom = dominators(entry, node_map.keys(), adjacency)
        back_pairs: set[tuple[str, str]] = set()
        for src in dom:
            for dst in adjacency.get(src, []):
                if dst in dom.get(src, set()):
                    back_pairs.add((src, dst))
        out_edges: dict[str, list[Edge]] = defaultdict(list)
        for edge in graph.edges:
            if edge.kind != "tool" and edge.enabled:
                out_edges[edge.source_node_id].append(edge)
        body_nodes = set().union(*bodies.values())
        flagged: set[str] = set()
        for node_id in body_nodes:
            node_out = out_edges.get(node_id, [])
            if len(node_out) <= 1:
                continue  # one way out — no fork possible
            back_out = [
                edge
                for edge in node_out
                if (edge.source_node_id, edge.target_node_id) in back_pairs
            ]
            if len(back_out) >= 2:
                # Tail of two loops: >=2 back-edges re-enter >=2 headers at once,
                # forking a token into each loop. Their loop conditions cannot be
                # assumed disjoint, so this is always a fork.
                flagged.add(node_id)
                continue
            innermost = min(
                (b for b in bodies.values() if node_id in b), key=len, default=frozenset()
            )
            # OK-case 1 (diamond): every edge is an unconditional forward that
            # stays inside the innermost loop, AND every possible path from the
            # branches reaches one common in-loop node before an exit or
            # back-edge. Staying in the loop at the first hop is insufficient:
            # two arms may later leave for the same out-of-loop join (D3), where
            # the engine cannot accumulate their concurrently exiting tokens.
            diamond = all(
                edge.condition is None
                and (edge.source_node_id, edge.target_node_id) not in back_pairs
                and edge.target_node_id in innermost
                for edge in node_out
            ) and branches_reconverge_before_loop_boundary(
                node_out=node_out,
                innermost=innermost,
                back_pairs=back_pairs,
                out_edges=out_edges,
            )
            # OK-case 2 (clean decision): NO edge is unconditional, so the node
            # takes exactly one branch per visit (mutually-exclusive conditions,
            # incl. a conditional back-edge next to a conditional exit). We
            # cannot prove arbitrary conditions disjoint, so this trusts the
            # author — the same assumption the runtime makes.
            decision = all(edge.condition is not None for edge in node_out)
            if diamond or decision:
                continue
            # Otherwise: an unconditional edge (always active) sits beside
            # another edge that can also fire, forking the single token.
            flagged.add(node_id)
        for node_id in sorted(flagged):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.FANOUT_IN_LOOP,
                message=(
                    f"node {node_id!r} forks its loop: it has two or more "
                    "concurrently-active outgoing edges (an unconditional edge "
                    "and/or a back-edge) that do not all reconverge inside the "
                    "loop, splitting the single circulating token. The sequential "
                    "join barrier does not support fan-out inside a loop. Gate the "
                    "branches behind mutually-exclusive conditions so exactly one "
                    "is taken per visit, or move the fan-out outside the loop."
                ),
                graph_id=graph.graph_id,
                node_id=node_id,
            )


def branches_reconverge_before_loop_boundary(
    *,
    node_out: list[Edge],
    innermost: set[str],
    back_pairs: set[tuple[str, str]],
    out_edges: dict[str, list[Edge]],
) -> bool:
    """Prove fork arms necessarily meet before leaving/circulating the loop.

    A candidate reconvergence must be reachable from every successor using
    only in-loop, non-back edges. It must also postdominate every successor
    over that same region: after removing the candidate, reaching an exit,
    back-edge, dead end, or cycle proves that an arm can avoid reconvergence.
    Treating unknown conditions as possible paths is intentionally
    conservative; unsupported multi-token shapes must fail at publish time.
    """
    successors = [edge.target_node_id for edge in node_out]

    def reachable_before_boundary(start: str) -> set[str]:
        reachable: set[str] = set()
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            for edge in out_edges.get(node_id, []):
                pair = (edge.source_node_id, edge.target_node_id)
                if pair not in back_pairs and edge.target_node_id in innermost:
                    stack.append(edge.target_node_id)
        return reachable

    common = set.intersection(*(reachable_before_boundary(successor) for successor in successors))

    def candidate_postdominates(candidate: str) -> bool:
        # Remove the candidate and inspect everything still reachable from
        # an arm. Any boundary edge or dead end is an avoiding path. A cycle
        # in the residual graph is also an avoiding path, so require a DAG.
        residual: dict[str, list[str]] = {}
        stack = [successor for successor in successors if successor != candidate]
        while stack:
            node_id = stack.pop()
            if node_id in residual:
                continue
            edges = out_edges.get(node_id, [])
            if not edges:
                return False
            next_nodes: list[str] = []
            for edge in edges:
                if edge.target_node_id == candidate:
                    continue
                pair = (edge.source_node_id, edge.target_node_id)
                if pair in back_pairs or edge.target_node_id not in innermost:
                    return False
                next_nodes.append(edge.target_node_id)
                stack.append(edge.target_node_id)
            residual[node_id] = next_nodes

        indegree = {node_id: 0 for node_id in residual}
        for targets in residual.values():
            for target in targets:
                indegree[target] += 1
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            node_id = ready.pop()
            visited += 1
            for target in residual[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        return visited == len(residual)

    return any(candidate_postdominates(candidate) for candidate in common)

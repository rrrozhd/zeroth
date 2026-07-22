"""B9 token-engine static analysis and provenance tags (phase P1).

This module is the foundation of the token/provenance join engine that replaces
the counter-based loop-epoch model. It is PURE static graph analysis plus tag
arithmetic — it does NOT dispatch anything and does not touch the runtime's
current behaviour. The drive loop uses it in effective token mode; explicit
``sequential_join_enabled=False`` retains the legacy path.

Why tokens instead of a recomputed epoch counter: a correct loop-join needs two
facts the counter model recomputed locally and got wrong on some shape every
time — whether a loop is still live, and whether a suppressed loop-exit edge is
final. A provenance **tag** that TRAVELS WITH THE TOKEN turns both into local,
by-construction facts: a token carries the iteration context of its enclosing
loops, a join matches tokens by tag, and a loop-exit edge is resolved only by the
exit-crossing event (P3), never per-iteration.

Everything here is defined only for REDUCIBLE graphs with a single latch per loop
header — both already enforced at publish (``IRREDUCIBLE_LOOP`` /
``MULTI_LATCH_LOOP``) when the flag is on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from zeroth.contracts.graph.models import Edge, Graph

# A provenance tag is the iteration context of a token's enclosing loops:
# a tuple of (loop_header, iteration) pairs, sorted by header for canonical
# identity and hashability. The empty tuple is the outermost (no-loop) scope.
TokenTag = tuple[tuple[str, int], ...]
INITIAL_TAG: TokenTag = ()


def tag_key(tag: TokenTag) -> str:
    """Stable string form of a tag for use as a JSON-checkpoint dict key."""
    return "|".join(f"{header}:{iteration}" for header, iteration in tag) or "()"


def _control_edges(graph: Graph) -> list[Edge]:
    """Non-tool, enabled edges — the ones that carry control flow."""
    return [edge for edge in graph.edges if edge.kind != "tool" and edge.enabled]


def back_edge_ids(graph: Graph) -> frozenset[str]:
    """DFS back-edge ids — edges whose target is an ancestor on the DFS stack.

    Matches ``RuntimeOrchestrator._back_edge_ids`` exactly (entry-first, then
    remaining nodes in declaration order) so the token engine and the legacy
    engine classify loops identically. On a reducible graph this set is
    DFS-order-independent (dominator-based back-edges), so the ordering only
    matters for the (rejected-at-publish) irreducible case.
    """
    outgoing: dict[str, list[Edge]] = defaultdict(list)
    for edge in _control_edges(graph):
        outgoing[edge.source_node_id].append(edge)

    on_stack, done = 0, 1
    back: set[str] = set()
    state: dict[str, int] = {}
    roots = [graph.entry_step] if graph.entry_step else []
    roots += [node.node_id for node in graph.nodes]
    for root in roots:
        if root is None or root in state:
            continue
        state[root] = on_stack
        stack: list[tuple[str, Iterator[Edge]]] = [(root, iter(outgoing[root]))]
        while stack:
            node_id, edge_iter = stack[-1]
            descended = False
            for edge in edge_iter:
                target_state = state.get(edge.target_node_id)
                if target_state == on_stack:
                    back.add(edge.edge_id)
                elif target_state is None:
                    state[edge.target_node_id] = on_stack
                    stack.append((edge.target_node_id, iter(outgoing[edge.target_node_id])))
                    descended = True
                    break
            if not descended:
                state[node_id] = done
                stack.pop()
    return frozenset(back)


def loop_bodies(graph: Graph, back_ids: frozenset[str] | None = None) -> dict[str, frozenset[str]]:
    """Map each loop header to its natural-loop body.

    A loop header H is the target of a back-edge ``tail -> H``. Its natural loop
    is H plus every node that can reach ``tail`` without passing through H. Bodies
    of back-edges that share a header are unioned (a single latch per header is
    enforced at publish, so in practice there is one back-edge per header).
    """
    back = back_edge_ids(graph) if back_ids is None else back_ids
    preds: dict[str, list[str]] = defaultdict(list)
    for edge in _control_edges(graph):
        preds[edge.target_node_id].append(edge.source_node_id)

    bodies: dict[str, set[str]] = defaultdict(set)
    for edge in _control_edges(graph):
        if edge.edge_id not in back:
            continue
        header, tail = edge.target_node_id, edge.source_node_id
        # Natural-loop of tail->header (dragon-book): the header, plus every node
        # that can reach the tail WITHOUT passing through the header. The reverse
        # walk starts at the tail and never expands the header (it is seeded into
        # ``body`` but never pushed), so the loop's entry predecessors stay out —
        # in particular a self-loop (tail == header) yields just {header}.
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
    return {header: frozenset(body) for header, body in bodies.items()}


def node_enclosing_loops(
    graph: Graph, bodies: dict[str, frozenset[str]] | None = None
) -> dict[str, frozenset[str]]:
    """Map each node to the set of loop headers whose body contains it."""
    loop_bodies_ = loop_bodies(graph) if bodies is None else bodies
    enclosing: dict[str, set[str]] = defaultdict(set)
    for header, body in loop_bodies_.items():
        for node in body:
            enclosing[node].add(header)
    return {node: frozenset(headers) for node, headers in enclosing.items()}


def loop_exit_edges(
    graph: Graph, bodies: dict[str, frozenset[str]] | None = None
) -> dict[str, frozenset[str]]:
    """Map each loop header to the ids of its EXIT edges.

    An exit edge of loop L leaves the loop: its source is in L's body and its
    target is not. These are the ONLY edges the loop-scope termination event
    (P3) resolves at the outer tag; a loop-body node never resolves its own exit
    edge by ordinary edge resolution.
    """
    loop_bodies_ = loop_bodies(graph) if bodies is None else bodies
    exits: dict[str, set[str]] = {header: set() for header in loop_bodies_}
    for edge in _control_edges(graph):
        for header, body in loop_bodies_.items():
            if edge.source_node_id in body and edge.target_node_id not in body:
                exits[header].add(edge.edge_id)
    return {header: frozenset(edge_ids) for header, edge_ids in exits.items()}


def exit_edge_owners(
    bodies: dict[str, frozenset[str]], exit_edges: dict[str, frozenset[str]]
) -> dict[str, str]:
    """Map each exit edge to the OUTERMOST loop it exits — its unique unit owner.

    An edge may exit several loops at once (a single edge leaving two nested
    loops). Natural loops nest, so the loops an edge exits form a chain and the
    outermost (largest body) is well-defined. Assigning each exit edge to exactly
    one loop's termination unit — the outermost — is what stops a nested
    double-exit edge from orphaning: crossing the inner loop never touches it, and
    it resolves exactly once, when the outermost loop it exits terminates.
    """
    owners: dict[str, str] = {}
    for header, edge_ids in exit_edges.items():
        for edge_id in edge_ids:
            current = owners.get(edge_id)
            if current is None or len(bodies[header]) > len(bodies[current]):
                owners[edge_id] = header
    return owners


@dataclass(frozen=True)
class GraphScopes:
    """Cached static loop analysis for one graph version."""

    back_edges: frozenset[str]
    bodies: dict[str, frozenset[str]]
    enclosing: dict[str, frozenset[str]]
    exit_edges: dict[str, frozenset[str]]
    # Each exit edge -> the outermost loop it exits (its termination-unit owner).
    exit_owner: dict[str, str]

    def loops_of(self, node_id: str) -> frozenset[str]:
        return self.enclosing.get(node_id, frozenset())


def analyze(graph: Graph) -> GraphScopes:
    """Compute all static loop analysis for a graph in one pass."""
    back = back_edge_ids(graph)
    bodies = loop_bodies(graph, back)
    exit_edges = loop_exit_edges(graph, bodies)
    return GraphScopes(
        back_edges=back,
        bodies=bodies,
        enclosing=node_enclosing_loops(graph, bodies),
        exit_edges=exit_edges,
        exit_owner=exit_edge_owners(bodies, exit_edges),
    )


def propagate_tag(source_tag: TokenTag, edge: Edge, scopes: GraphScopes) -> TokenTag:
    """The tag a token carries after crossing ``edge`` from its source.

    Local, by-construction rules over the static loop nesting:
    * a loop the target ENTERS (target in it, source not) → fresh ``(header, 0)``;
    * a loop BOTH are in → keep the source's iteration, but if this edge is that
      loop's back-edge (re-entry), increment it;
    * a loop the source EXITS (source in it, target not) → drop that component.

    So a back-edge bumps exactly its own loop's iteration; an exit edge strips the
    exited loop(s) and the token drops to the outer scope; a plain forward edge
    inside one loop nest leaves the tag unchanged.
    """
    source_map = dict(source_tag)
    enc_source = scopes.loops_of(edge.source_node_id)
    enc_target = scopes.loops_of(edge.target_node_id)
    # A back-edge re-enters the loop headed by its target.
    reentered = edge.target_node_id if edge.edge_id in scopes.back_edges else None
    new_map: dict[str, int] = {}
    for header in enc_target:
        if header in enc_source:
            iteration = source_map.get(header, 0)
            if header == reentered:
                iteration += 1
            new_map[header] = iteration
        else:
            new_map[header] = 0
    return tuple(sorted(new_map.items()))


def nodes_in_any_loop(scopes: GraphScopes) -> frozenset[str]:
    """All nodes that sit inside at least one loop body."""
    return frozenset(scopes.enclosing.keys())
